import { NextRequest, NextResponse } from "next/server";
import { appendNewSite } from "@/lib/agents";
import { guardLocalApiRequest, readJsonBody } from "@/lib/local-api-security";

export const dynamic = "force-dynamic";

export async function POST(req: NextRequest) {
  const denied = guardLocalApiRequest(req, { requireOrigin: true });
  if (denied) return denied;
  let body: Record<string, unknown>;
  try {
    body = await readJsonBody(req, 8_192);
  } catch {
    return NextResponse.json({ error: "invalid request" }, { status: 400 });
  }

  const urlRaw = typeof body.url === "string" ? body.url.trim() : "";
  const name = typeof body.name === "string" ? body.name.trim().slice(0, 120) : "";
  const connections = Array.isArray(body.connections)
    ? body.connections
      .filter((item): item is string => typeof item === "string")
      .map((item) => item.trim().slice(0, 80))
      .filter(Boolean)
      .slice(0, 12)
    : [];
  let url = "";
  try {
    const parsed = new URL(urlRaw);
    if (
      !["http:", "https:"].includes(parsed.protocol) ||
      parsed.username ||
      parsed.password ||
      parsed.hash ||
      urlRaw.length > 2_048
    ) throw new Error("invalid URL");
    url = parsed.toString();
  } catch {
    // handled by the common validation response below
  }

  if (!url || !name || /[\u0000-\u001f\u007f]/.test(name + connections.join(""))) {
    return NextResponse.json(
      { error: "invalid", message: "Нужны корректные название и URL сайта." },
      { status: 400 }
    );
  }

  try {
    const task = appendNewSite({ url, name, connections });
    return NextResponse.json({ ok: true, task });
  } catch {
    return NextResponse.json(
      { error: "append_failed", message: "Не удалось добавить задачу." },
      { status: 503 }
    );
  }
}
