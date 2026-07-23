import "server-only";

import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import os from "node:os";
import { StringDecoder } from "node:string_decoder";

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "::1", "localhost"]);
const DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin";

export type LocalProcessErrorKind =
  | "aborted"
  | "invalid-output"
  | "output-limit"
  | "spawn"
  | "timeout"
  | "exit";

export class LocalProcessError extends Error {
  public readonly kind: LocalProcessErrorKind;

  constructor(kind: LocalProcessErrorKind, message: string) {
    super(message);
    this.name = "LocalProcessError";
    this.kind = kind;
  }
}

function isLoopbackHostname(hostname: string): boolean {
  return LOOPBACK_HOSTS.has(hostname.toLowerCase().replace(/^\[|\]$/g, ""));
}

function parseHostHeader(value: string | null): URL | null {
  if (!value || /[/?#@\\]/.test(value)) return null;
  try {
    return new URL(`http://${value}`);
  } catch {
    return null;
  }
}

/**
 * Dashboard API routes are intentionally local-only. Binding Next to loopback is
 * the primary boundary; this check also rejects Host-header/DNS-rebinding and
 * cross-origin browser requests if the process is ever started differently.
 */
export function guardLocalApiRequest(
  req: Request,
  options: { requireOrigin?: boolean } = {},
): Response | null {
  let requestUrl: URL;
  try {
    requestUrl = new URL(req.url);
  } catch {
    return Response.json({ ok: false, error: "forbidden" }, { status: 403 });
  }

  const host = parseHostHeader(req.headers.get("host"));
  const requestOrigin = `${requestUrl.protocol}//${host?.host ?? ""}`;
  if (
    !["http:", "https:"].includes(requestUrl.protocol) ||
    !host ||
    !isLoopbackHostname(host.hostname)
  ) {
    return Response.json({ ok: false, error: "forbidden" }, { status: 403 });
  }

  const fetchSite = req.headers.get("sec-fetch-site");
  if (fetchSite && fetchSite !== "same-origin" && fetchSite !== "none") {
    return Response.json({ ok: false, error: "forbidden" }, { status: 403 });
  }

  const origin = req.headers.get("origin");
  if (options.requireOrigin && !origin) {
    return Response.json({ ok: false, error: "forbidden" }, { status: 403 });
  }
  if (origin) {
    try {
      const originUrl = new URL(origin);
      if (
        origin !== originUrl.origin ||
        !isLoopbackHostname(originUrl.hostname) ||
        originUrl.origin !== requestOrigin
      ) {
        return Response.json({ ok: false, error: "forbidden" }, { status: 403 });
      }
    } catch {
      return Response.json({ ok: false, error: "forbidden" }, { status: 403 });
    }
  }

  return null;
}

export async function readJsonBody(
  req: Request,
  maxBytes: number,
): Promise<Record<string, unknown>> {
  const contentType = req.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase();
  if (contentType !== "application/json") {
    throw new LocalProcessError("invalid-output", "JSON content type required");
  }

  const contentLength = req.headers.get("content-length");
  if (contentLength) {
    const declaredLength = Number(contentLength);
    if (
      !Number.isSafeInteger(declaredLength) ||
      declaredLength < 0 ||
      declaredLength > maxBytes
    ) {
      throw new LocalProcessError("output-limit", "Request body is too large");
    }
  }

  const reader = req.body?.getReader();
  const chunks: Uint8Array[] = [];
  let received = 0;
  if (reader) {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      received += value.byteLength;
      if (received > maxBytes) {
        await reader.cancel();
        throw new LocalProcessError("output-limit", "Request body is too large");
      }
      chunks.push(value);
    }
  }

  const raw = new TextDecoder("utf-8", { fatal: true }).decode(
    Buffer.concat(chunks, received),
  );
  const parsed: unknown = JSON.parse(raw);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new LocalProcessError("invalid-output", "JSON object required");
  }
  return parsed as Record<string, unknown>;
}

/**
 * Never forward the Next.js process environment wholesale: it can contain API
 * keys that the spawned Python/Codex process does not need.
 */
export function sanitizedPythonEnv(): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {
    HOME: os.homedir(),
    NODE_ENV: process.env.NODE_ENV ?? "production",
    PATH: process.env.PATH || DEFAULT_PATH,
    PYTHONUNBUFFERED: "1",
  };
  for (const key of [
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "CODEX_HOME",
    "CODEX_BIN",
    "MRSEO_CODEX_MODEL",
  ]) {
    const value = process.env[key];
    if (value) env[key] = value;
  }
  return env;
}

function signalProcessGroup(
  child: ChildProcessWithoutNullStreams,
  signal: NodeJS.Signals,
): void {
  if (!child.pid) return;
  if (process.platform !== "win32") {
    try {
      process.kill(-child.pid, signal);
      return;
    } catch {
      // The child may have exited between the state check and kill.
    }
  }
  try {
    child.kill(signal);
  } catch {
    // Already gone.
  }
}

interface RunLocalProcessOptions {
  cwd: string;
  input?: string;
  maxStdoutBytes?: number;
  signal?: AbortSignal;
  timeoutMs: number;
}

export async function runLocalProcess(
  executable: string,
  args: string[],
  options: RunLocalProcessOptions,
): Promise<string> {
  const maxStdoutBytes = options.maxStdoutBytes ?? 1_048_576;

  return new Promise<string>((resolve, reject) => {
    if (options.signal?.aborted) {
      reject(new LocalProcessError("aborted", "Request aborted"));
      return;
    }

    const child = spawn(executable, args, {
      cwd: options.cwd,
      detached: process.platform !== "win32",
      env: sanitizedPythonEnv(),
      stdio: ["pipe", "pipe", "pipe"],
    });

    let settled = false;
    let stdout = "";
    let stdoutBytes = 0;
    const stdoutDecoder = new StringDecoder("utf8");
    let forceKillTimer: NodeJS.Timeout | null = null;

    const cleanup = () => {
      clearTimeout(timer);
      options.signal?.removeEventListener("abort", onAbort);
    };
    const fail = (error: LocalProcessError, kill = true) => {
      if (settled) return;
      settled = true;
      if (kill) {
        // Give Python a short window to forward SIGTERM and reap its own Codex
        // process group. SIGKILL is only the bounded fallback.
        signalProcessGroup(child, "SIGTERM");
        forceKillTimer = setTimeout(() => signalProcessGroup(child, "SIGKILL"), 1_500);
        forceKillTimer.unref();
      }
      cleanup();
      reject(error);
    };
    const onAbort = () => fail(new LocalProcessError("aborted", "Request aborted"));
    const timer = setTimeout(
      () => fail(new LocalProcessError("timeout", "Child process timed out")),
      options.timeoutMs,
    );
    timer.unref();

    options.signal?.addEventListener("abort", onAbort, { once: true });
    child.stdout.on("data", (chunk: Buffer) => {
      stdoutBytes += chunk.byteLength;
      if (stdoutBytes > maxStdoutBytes) {
        fail(new LocalProcessError("output-limit", "Child output exceeded limit"));
        return;
      }
      stdout += stdoutDecoder.write(chunk);
    });
    // Drain stderr so a noisy child cannot deadlock, but never expose it to HTTP clients.
    child.stderr.resume();
    child.stdin.on("error", () => {
      // EPIPE is expected when a child exits before consuming all input.
    });
    child.once("error", () => {
      fail(new LocalProcessError("spawn", "Could not start child process"), false);
    });
    child.once("close", (code, signal) => {
      // When termination was requested, keep the delayed SIGKILL fallback:
      // the Python leader may exit before an uncooperative grandchild.
      if (settled) return;
      if (forceKillTimer) {
        clearTimeout(forceKillTimer);
        forceKillTimer = null;
      }
      settled = true;
      cleanup();
      if (code !== 0) {
        reject(
          new LocalProcessError(
            "exit",
            `Child process exited with ${signal ? "signal" : "code"} ${signal ?? code ?? "unknown"}`,
          ),
        );
        return;
      }
      stdout += stdoutDecoder.end();
      resolve(stdout);
    });

    if (options.signal?.aborted) {
      onAbort();
      return;
    }
    child.stdin.end(options.input ?? "");
  });
}
