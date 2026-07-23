import { NextRequest } from "next/server";
import path from "node:path";
import {
  guardLocalApiRequest,
  readJsonBody,
  runLocalProcess,
} from "@/lib/local-api-security";

export const dynamic = "force-dynamic";
export const maxDuration = 300;

/** «Объясни цифру»: POST {site, metric, value, context?} → короткое объяснение
 *  мозгом в контексте живых данных (тот же orchestrator chat). */
const SEO_AGENT_ROOT = process.env.SEO_AGENT_ROOT ?? path.resolve(process.cwd(), "..");
const SITES = new Set(["mysite", "demo2", "demo3"]);

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
  const metric = typeof body.metric === "string" ? body.metric.trim().slice(0, 120) : "";
  const value = typeof body.value === "string" ? body.value.trim().slice(0, 60) : "";
  const context = typeof body.context === "string" ? body.context.trim().slice(0, 200) : "";
  const lang = body.lang === "en" ? "en" : body.lang === "ru" ? "ru" : null;
  const combined = metric + value + context;
  if (!SITES.has(site) || !lang || !metric || /[\u0000-\u001f\u007f]/.test(combined)) {
    return Response.json({ ok: false, error: "invalid request" }, { status: 400 });
  }

  const langLine = lang === "en" ? "Отвечай на английском языке." : "Отвечай на русском языке.";
  const q = `${langLine} [Пользователь смотрит сайт: ${site}] Объясни ОДНУ цифру просто и коротко (2-4 предложения, без приветствий): метрика «${metric}» = ${value}${context ? `, контекст: ${context}` : ""}. Что это значит для меня и хорошо это или плохо?`;
  const py = path.join(SEO_AGENT_ROOT, "venv", "bin", "python");
  try {
    const text = await runLocalProcess(
      py,
      [path.join(SEO_AGENT_ROOT, "swarm", "orchestrator.py"), "chat"],
      {
        cwd: SEO_AGENT_ROOT,
        input: q,
        maxStdoutBytes: 131_072,
        signal: req.signal,
        timeoutMs: 250_000,
      },
    );
    return new Response(text.slice(0, 50_000), {
      headers: {
        "Content-Type": "text/plain; charset=utf-8",
        "Cache-Control": "no-store",
      },
    });
  } catch {
    return new Response(
      lang === "en" ? "brain temporarily unavailable" : "мозг временно недоступен",
      { status: 503 },
    );
  }
}
