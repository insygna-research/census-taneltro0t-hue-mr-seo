import { NextRequest, NextResponse } from "next/server";
import { appendTask, readTasks, readWorkerStatus } from "@/lib/agents";
import { taskCore } from "@/lib/utils";
import { guardLocalApiRequest, readJsonBody } from "@/lib/local-api-security";

export const dynamic = "force-dynamic";

export function GET(req: NextRequest) {
  const denied = guardLocalApiRequest(req);
  if (denied) return denied;
  try {
    return NextResponse.json({ ...readTasks(), worker: readWorkerStatus() });
  } catch (e) {
    return NextResponse.json({ error: "tasks_failed", message: String(e) }, { status: 500 });
  }
}

export async function POST(req: NextRequest) {
  const denied = guardLocalApiRequest(req, { requireOrigin: true });
  if (denied) return denied;
  let body: Record<string, unknown>;
  try {
    body = await readJsonBody(req, 4_096);
  } catch {
    return NextResponse.json({ error: "invalid request" }, { status: 400 });
  }
  const text = typeof body.text === "string" ? body.text.trim() : "";
  if (!text || text.length > 1_000 || /[\u0000-\u001f\u007f]/.test(text)) {
    return NextResponse.json({ error: "invalid task" }, { status: 400 });
  }
  try {
    // дедуп: такая же незакрытая задача уже в очереди → не плодим
    const existing = readTasks().tasks.find((q) => q.status === "queued" && taskCore(q.text) === taskCore(text));
    if (existing) {
      return NextResponse.json({ ok: true, dedup: true, task: existing });
    }
    const task = appendTask(text);
    return NextResponse.json({ ok: true, task });
  } catch {
    return NextResponse.json(
      { error: "append_failed", message: "Не удалось добавить задачу." },
      { status: 503 },
    );
  }
}
