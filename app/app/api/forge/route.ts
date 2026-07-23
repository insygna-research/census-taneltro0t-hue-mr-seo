import { NextRequest } from "next/server";
import path from "node:path";
import { domainToASCII } from "node:url";

import {
  guardLocalApiRequest,
  readJsonBody,
  runLocalProcess,
} from "@/lib/local-api-security";

export const dynamic = "force-dynamic";
export const maxDuration = 300;
export const runtime = "nodejs";

const SEO_AGENT_ROOT =
  process.env.SEO_AGENT_ROOT ?? path.resolve(process.cwd(), "..");
const PY = path.join(SEO_AGENT_ROOT, "venv", "bin", "python");
const FORGE = path.join(SEO_AGENT_ROOT, "swarm", "content_forge.py");
const FORGE_TIMEOUT_MS = 265_000;
const SITES = new Set(["mysite", "demo2", "demo3"]);
const SITE_HOSTS: Record<string, Set<string>> = {
  mysite: new Set(["example.com", "www.example.com"]),
  demo2: new Set(["example.org", "www.example.org"]),
  demo3: new Set(["example.net"]),
};

function safePageUrl(site: string, value: unknown): string | null {
  if (value === undefined || value === null || value === "") return "";
  if (
    typeof value !== "string" ||
    value.length > 300 ||
    /[\u0000-\u001f\u007f]/.test(value)
  ) {
    return null;
  }

  try {
    const allowedHosts = SITE_HOSTS[site];
    const isRelative = value.startsWith("/") && !value.startsWith("//");
    const url = new URL(value, `https://${[...allowedHosts][0]}`);
    if (
      url.protocol !== "https:" ||
      url.port ||
      url.username ||
      url.password ||
      url.hash ||
      !allowedHosts.has(domainToASCII(url.hostname).toLowerCase())
    ) {
      return null;
    }
    return isRelative ? `${url.pathname}${url.search}` : url.toString();
  } catch {
    return null;
  }
}

export async function POST(req: NextRequest) {
  const denied = guardLocalApiRequest(req, { requireOrigin: true });
  if (denied) return denied;

  let body: Record<string, unknown>;
  try {
    body = await readJsonBody(req, 4_096);
  } catch {
    return Response.json({ ok: false, error: "invalid request" }, { status: 400 });
  }

  const site = typeof body.site === "string" ? body.site : "";
  const query = typeof body.query === "string" ? body.query.trim() : "";
  const url = SITES.has(site) ? safePageUrl(site, body.url) : null;
  if (
    !SITES.has(site) ||
    query.length < 3 ||
    query.length > 200 ||
    /[\u0000-\u001f\u007f]/.test(query) ||
    url === null
  ) {
    return Response.json({ ok: false, error: "invalid site/query/url" }, { status: 400 });
  }

  const args = [FORGE, site, query];
  if (url) args.push(url);

  try {
    const stdout = await runLocalProcess(PY, args, {
      cwd: SEO_AGENT_ROOT,
      maxStdoutBytes: 262_144,
      signal: req.signal,
      timeoutMs: FORGE_TIMEOUT_MS,
    });
    const payload: unknown = JSON.parse(stdout.trim());
    if (
      !payload ||
      typeof payload !== "object" ||
      !("ok" in payload) ||
      payload.ok !== true ||
      !("draft_path" in payload) ||
      typeof payload.draft_path !== "string" ||
      !payload.draft_path.startsWith(`content_drafts/${site}/`) ||
      !("chars" in payload) ||
      typeof payload.chars !== "number" ||
      !Number.isFinite(payload.chars)
    ) {
      throw new Error("invalid forge response");
    }
    return Response.json({
      ok: true,
      draft_path: payload.draft_path,
      chars: payload.chars,
    }, {
      headers: { "Cache-Control": "no-store" },
    });
  } catch {
    return Response.json(
      { ok: false, error: "генерация временно недоступна" },
      { status: 503 },
    );
  }
}
