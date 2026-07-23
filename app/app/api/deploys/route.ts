import { NextRequest } from "next/server";
import path from "node:path";
import {
  guardLocalApiRequest,
  readJsonBody,
  runLocalProcess,
} from "@/lib/local-api-security";

export const dynamic = "force-dynamic";
export const maxDuration = 180;

/** Конвейер деплоев роя.
 *  GET  /api/deploys → {pending, merged}
 *  POST /api/deploys {site, branch} → merge в 1 клик (только mrseo/*) */
const SEO_AGENT_ROOT = process.env.SEO_AGENT_ROOT ?? path.resolve(process.cwd(), "..");
const PY = path.join(SEO_AGENT_ROOT, "venv", "bin", "python");
const SCRIPT = path.join(SEO_AGENT_ROOT, "swarm", "deploys.py");
const SITES = new Set(["mysite", "demo2", "demo3"]);
const BRANCH_RE = /^mrseo\/[A-Za-z0-9._\/-]{2,60}$/;

export async function GET(req: NextRequest) {
  const denied = guardLocalApiRequest(req);
  if (denied) return denied;
  try {
    const stdout = await runLocalProcess(PY, [SCRIPT, "list"], {
      cwd: SEO_AGENT_ROOT,
      signal: req.signal,
      timeoutMs: 60_000,
    });
    return Response.json(JSON.parse(stdout.trim()));
  } catch {
    return Response.json({ error: "операция временно недоступна" }, { status: 503 });
  }
}

export async function POST(req: NextRequest) {
  const denied = guardLocalApiRequest(req, { requireOrigin: true });
  if (denied) return denied;
  try {
    const body = await readJsonBody(req, 4_096);
    const site = typeof body.site === "string" ? body.site : "";
    const branch = typeof body.branch === "string" ? body.branch : "";
    if (!SITES.has(site) || !BRANCH_RE.test(branch)) {
      return Response.json({ ok: false, error: "неверные site/branch" }, { status: 400 });
    }
    const stdout = await runLocalProcess(PY, [SCRIPT, "merge", site, branch], {
      cwd: SEO_AGENT_ROOT,
      signal: req.signal,
      timeoutMs: 170_000,
    });
    return Response.json(JSON.parse(stdout.trim()));
  } catch {
    return Response.json({ ok: false, error: "операция временно недоступна" }, { status: 503 });
  }
}
