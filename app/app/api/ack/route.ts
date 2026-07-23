import { NextRequest, NextResponse } from "next/server";
import fs from "node:fs";
import path from "node:path";
import { SEO_ROOT } from "@/lib/fs-data";
import { guardLocalApiRequest, readJsonBody } from "@/lib/local-api-security";

export const dynamic = "force-dynamic";

/** Подтверждение «сделано» для карточек с внешним действием (отзывы и т.п.).
 *  Ключ несёт версию состояния (например review:site:56) — новое событие
 *  меняет ключ, и карточка честно возвращается. */
const ACKS = path.join(SEO_ROOT, "swarm/tasks/acks.json");

export async function POST(req: NextRequest) {
  const denied = guardLocalApiRequest(req, { requireOrigin: true });
  if (denied) return denied;
  let body: Record<string, unknown>;
  try {
    body = await readJsonBody(req, 1_024);
  } catch {
    return NextResponse.json({ ok: false, error: "bad request" }, { status: 400 });
  }
  const key = typeof body.key === "string" ? body.key.trim().slice(0, 160) : "";
  if (!key || !/^[a-zа-яё0-9:_,.\-\s«»()]+$/i.test(key)) {
    return NextResponse.json({ ok: false, error: "bad key" }, { status: 400 });
  }
  let d: Record<string, string> = {};
  try { d = JSON.parse(fs.readFileSync(ACKS, "utf8")); } catch {}
  d[key] = new Date().toISOString();
  fs.mkdirSync(path.dirname(ACKS), { recursive: true });
  fs.writeFileSync(ACKS, JSON.stringify(d, null, 1));
  return NextResponse.json({ ok: true });
}
