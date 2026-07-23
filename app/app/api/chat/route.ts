import { NextRequest } from "next/server";
import path from "node:path";

import {
  guardLocalApiRequest,
  readJsonBody,
  runLocalProcess,
} from "@/lib/local-api-security";

export const dynamic = "force-dynamic";
export const maxDuration = 300;
export const runtime = "nodejs";

/**
 * Мозг Mr.Seo: вопрос → swarm/assistant.py → headless Codex CLI.
 *
 * Контракт: POST { message, site, lang, thread } → text/plain.
 * Эндпоинт намеренно доступен только с локального dashboard origin.
 */
const SEO_AGENT_ROOT =
  process.env.SEO_AGENT_ROOT ?? path.resolve(process.cwd(), "..");
const CHAT_TIMEOUT_MS = 250_000;
const THREAD_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

const SITE_LABEL = {
  mysite: "example.com (студия звукозаписи, Столица + Город)",
  demo2: "example.org (клуб, Город)",
  demo3: "РЦ Основа (реабилитационный центр, Город-2)",
} as const;

type Site = keyof typeof SITE_LABEL;

function isSite(value: unknown): value is Site {
  return typeof value === "string" && Object.hasOwn(SITE_LABEL, value);
}

function textResponse(text: string, status = 200): Response {
  return new Response(text, {
    status,
    headers: {
      "Content-Type": "text/plain; charset=utf-8",
      "Cache-Control": "no-store, no-transform",
    },
  });
}

export async function POST(req: NextRequest) {
  const denied = guardLocalApiRequest(req, { requireOrigin: true });
  if (denied) return denied;

  let body: Record<string, unknown>;
  try {
    body = await readJsonBody(req, 12_000);
  } catch {
    return Response.json({ ok: false, error: "invalid request" }, { status: 400 });
  }

  const message = typeof body.message === "string" ? body.message.trim() : "";
  const site = body.site;
  const lang = body.lang === "en" ? "en" : body.lang === "ru" ? "ru" : null;
  const thread = typeof body.thread === "string" ? body.thread : "";
  if (
    !isSite(site) ||
    !lang ||
    !THREAD_RE.test(thread) ||
    message.length > 2_000
  ) {
    return Response.json({ ok: false, error: "invalid request" }, { status: 400 });
  }

  if (!message) {
    return textResponse(
      lang === "en"
        ? "Hi. I'm Mr.Seo — I watch your sites' live data. Ask me, for example: “why isn't Moscow growing?” or “what should I do this week?”."
        : "Привет. Я Mr.Seo — вижу свежие данные ваших сайтов. Спросите, например: «почему Столица не растёт?» или «что сделать на этой неделе?».",
    );
  }

  const langLine =
    lang === "en" ? "Отвечай на английском языке." : "Отвечай на русском языке.";
  const question = `${langLine}\n[Пользователь смотрит сайт: ${SITE_LABEL[site]}]\n${message}`;
  const py = path.join(SEO_AGENT_ROOT, "venv", "bin", "python");
  const script = path.join(SEO_AGENT_ROOT, "swarm", "assistant.py");

  try {
    const stdout = await runLocalProcess(
      py,
      [script, "chat", "--thread", `app-${thread}`],
      {
        cwd: SEO_AGENT_ROOT,
        input: question,
        maxStdoutBytes: 262_144,
        signal: req.signal,
        timeoutMs: CHAT_TIMEOUT_MS,
      },
    );
    const payload: unknown = JSON.parse(stdout.trim());
    const text =
      payload &&
      typeof payload === "object" &&
      "ok" in payload &&
      payload.ok === true &&
      "text" in payload &&
      typeof payload.text === "string"
        ? payload.text.trim()
        : "";
    if (!text) throw new Error("empty assistant response");
    return textResponse(text.slice(0, 100_000));
  } catch {
    return textResponse(
      lang === "en"
        ? "The brain is temporarily unavailable. Dashboard data remains live — please try again shortly."
        : "Мозг временно недоступен. Данные на дашборде остаются актуальными — попробуйте ещё раз чуть позже.",
      503,
    );
  }
}
