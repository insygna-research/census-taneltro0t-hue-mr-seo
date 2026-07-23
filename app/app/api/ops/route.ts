import { NextRequest } from "next/server";
import path from "node:path";
import { domainToASCII } from "node:url";

import {
  guardLocalApiRequest,
  readJsonBody,
  runLocalProcess,
} from "@/lib/local-api-security";

export const dynamic = "force-dynamic";
export const maxDuration = 120;
export const runtime = "nodejs";

/**
 * Локальный пульт экосистемы через swarm/ops.py.
 * GET  /api/ops          → статус токенов/источников
 * POST /api/ops {action} → строго разрешённая операция
 */
const SEO_AGENT_ROOT =
  process.env.SEO_AGENT_ROOT ?? path.resolve(process.cwd(), "..");
const PY = path.join(SEO_AGENT_ROOT, "venv", "bin", "python");
const OPS = path.join(SEO_AGENT_ROOT, "swarm", "ops.py");
const ALLOWED_SITES = new Set(["mysite", "demo2", "demo3"]);
const SITE_HOSTS: Record<string, Set<string>> = {
  mysite: new Set(["example.com", "www.example.com"]),
  demo2: new Set(["example.org", "www.example.org"]),
  demo3: new Set(["example.net"]),
};

async function ops(
  args: string[],
  options: { input?: string; signal?: AbortSignal; timeoutMs?: number } = {},
): Promise<unknown> {
  const stdout = await runLocalProcess(PY, [OPS, ...args], {
    cwd: SEO_AGENT_ROOT,
    input: options.input,
    maxStdoutBytes: 262_144,
    signal: options.signal,
    timeoutMs: options.timeoutMs ?? 90_000,
  });
  return JSON.parse(stdout.trim());
}

function json(data: unknown, status = 200): Response {
  return Response.json(data, {
    status,
    headers: { "Cache-Control": "no-store" },
  });
}

function siteFrom(value: unknown): string | null {
  return typeof value === "string" && ALLOWED_SITES.has(value) ? value : null;
}

function siteUrl(site: string, value: unknown): string | null {
  if (typeof value !== "string" || value.length > 2_048) return null;
  try {
    const url = new URL(value);
    if (
      url.protocol !== "https:" ||
      url.port ||
      url.username ||
      url.password ||
      url.hash ||
      !SITE_HOSTS[site]?.has(domainToASCII(url.hostname).toLowerCase())
    ) {
      return null;
    }
    return url.toString();
  } catch {
    return null;
  }
}

let statusCache: { at: number; data: unknown } | null = null;
const STATUS_TTL_MS = 60_000;

export async function GET(req: NextRequest) {
  const denied = guardLocalApiRequest(req);
  if (denied) return denied;

  try {
    if (statusCache && Date.now() - statusCache.at < STATUS_TTL_MS) {
      return json(statusCache.data);
    }
    const data = await ops(["status"], { signal: req.signal });
    statusCache = { at: Date.now(), data };
    return json(data);
  } catch {
    return json({ ok: false, error: "операция временно недоступна" }, 503);
  }
}

export async function POST(req: NextRequest) {
  const denied = guardLocalApiRequest(req, { requireOrigin: true });
  if (denied) return denied;

  let body: Record<string, unknown>;
  try {
    body = await readJsonBody(req, 8_192);
  } catch {
    return json({ ok: false, error: "неверный запрос" }, 400);
  }

  const action = typeof body.action === "string" ? body.action : "";
  try {
    if (action === "gsc_reauth") {
      return json(await ops(["gsc_reauth"], { signal: req.signal }));
    }

    if (action === "recrawl" || action === "indexnow") {
      const site = siteFrom(body.site);
      const url = site ? siteUrl(site, body.url) : null;
      if (!site || !url) {
        return json({ ok: false, error: "неверный site/url" }, 400);
      }
      return json(await ops([action, site, url], { signal: req.signal }));
    }

    if (
      action === "recrawl_quota" ||
      action === "aibots"
    ) {
      const site = siteFrom(body.site);
      if (!site) return json({ ok: false, error: "неверный site" }, 400);
      return json(await ops([action, site], { signal: req.signal }));
    }

    if (action === "set_bing_key") {
      const key = typeof body.key === "string" ? body.key.trim() : "";
      if (!/^[A-Za-z0-9-]{16,128}$/.test(key)) {
        return json({ ok: false, error: "неверный формат ключа" }, 400);
      }
      // The secret is sent over stdin and is never exposed in the process argv.
      return json(
        await ops(["set_bing_key"], {
          input: `${key}\n`,
          signal: req.signal,
        }),
      );
    }

    return json({ ok: false, error: "неизвестное действие" }, 400);
  } catch {
    return json({ ok: false, error: "операция временно недоступна" }, 503);
  }
}
