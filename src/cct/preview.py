"""The preview pane: one session's transcript, summarised.

Transcripts grow to hundreds of megabytes, so a large file is read as a head
chunk (the metadata records sit at the top) plus a tail chunk (the turns worth
showing).
"""

from __future__ import annotations

import glob
import json
import os
import re
from datetime import datetime

from . import daemon, fuzzy, paths
from .layout import KW_RE, PR_KW, dw, fmt_age, fmt_ctx, fmt_dur
from .model import Snapshot
from .theme import STATE_STYLE, Row, Style

__all__ = ["render", "render_job", "wrap", "MSG_KEEP", "MSG_MAX", "TAIL", "HEAD"]

TAIL = 1500 * 1024
HEAD = 256 * 1024
MSG_MAX = 400
MSG_KEEP = 30
WRAP_SIGN = "↳ "

_MODEL_DATE = re.compile(r"^(.*)-\d{8}$")


def render(sid: str, snap: Snapshot, query: str | fuzzy.Query | None = None) -> list[Row]:
    """The preview for ``sid``, or the job state when there is no transcript."""
    q = fuzzy.parse(query) if isinstance(query, str) else query
    try:
        path = _find_path(sid, snap)
        rows = _render_transcript(sid, path, q) if path else render_job(sid)
        if not rows:
            rows = [[(f"no transcript and no job state for {sid[:8]}", Style.DIM)]]
        return rows
    except Exception as exc:
        return [[(f"preview unavailable: {type(exc).__name__}", Style.DIM)]]


def render_job(sid: str) -> list[Row]:
    """Fallback view for a session the daemon knows but no transcript covers."""
    js = daemon.read_json(os.path.join(paths.job_dir(sid), "state.json"))
    if not js:
        return []
    out: list[Row] = [
        [(str(js.get("name") or sid[:8]), Style.BOLD)],
        [(sid, Style.DIM)],
    ]
    state = js.get("state") or ""
    if state:
        out.append([(f"bg:{state}", STATE_STYLE.get(state, Style.NORMAL))])
    out.append([])
    for k in ("detail", "intent", "cwd", "worktreePath", "createdAt", "updatedAt"):
        v = js.get(k)
        if isinstance(v, (str, int, float)) and str(v).strip():
            out.append([(k, Style.DIM), (" " + " ".join(str(v).split())[:MSG_MAX], Style.NORMAL)])
    lines = daemon.job_timeline(sid)
    if lines:
        out.append([])
        out.append([("timeline", Style.DIM)])
        out.extend([(line[:MSG_MAX], Style.DIM)] for line in lines)
    return out


def wrap(rows: list[Row], width: int) -> list[Row]:
    """Hard-wrap spans to ``width``, marking continuations with a dim ``↳``."""
    if width <= dw(WRAP_SIGN):
        return list(rows)
    out: list[Row] = []
    for row in rows:
        out.extend(_wrap_row(row, width))
    return out


def _wrap_row(row: Row, width: int) -> list[Row]:
    sign_w = dw(WRAP_SIGN)
    out: list[Row] = []
    line: Row = []
    used = 0
    limit = width
    for text, style in row:
        for ch in text:
            c = dw(ch)
            if used + c > limit:
                out.append(line)
                line = [(WRAP_SIGN, Style.DIM)]
                used = sign_w
                limit = width
            if line and line[-1][1] is style:
                line[-1] = (line[-1][0] + ch, style)
            else:
                line.append((ch, style))
            used += c
    out.append(line)
    return out


def _find_path(sid: str, snap: Snapshot) -> str:
    s = snap.sessions.get(sid)
    if s is not None and s.path and os.path.exists(s.path):
        return s.path
    hits = glob.glob(os.path.join(paths.PROJECTS_DIR, "*", sid + ".jsonl"))
    return max(hits, key=os.path.getmtime) if hits else ""




def _records(buf: bytes, drop_last: bool) -> list[dict]:
    lines = buf.decode("utf-8", "replace").split("\n")
    if drop_last and lines:
        lines = lines[:-1]
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if isinstance(o, dict):
            out.append(o)
    return out


def _load(path: str) -> tuple[list[dict], list[dict], bool]:
    size = os.path.getsize(path)
    truncated = size > TAIL
    with open(path, "rb") as fh:
        if truncated:
            head = fh.read(HEAD)
            fh.seek(size - TAIL)
            fh.readline()
            tail = fh.read()
        else:
            head = b""
            tail = fh.read()
    return _records(head, True), _records(tail, False), truncated


def _text_of(o: dict) -> tuple[str, int]:
    content = (o.get("message") or {}).get("content")
    parts: list[str] = []
    tools = 0
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for blk in content:
            if not isinstance(blk, dict):
                continue
            bt = blk.get("type")
            if bt == "text":
                parts.append(blk.get("text") or "")
            elif bt in ("tool_use", "tool_result"):
                tools += 1
    return " ".join(" ".join(parts).split()), tools


def _short_model(m: str) -> str:
    s = m[len("claude-"):] if m.startswith("claude-") else m
    mm = _MODEL_DATE.match(s)
    if mm:
        s = mm.group(1)
        head, _, tail = s.rpartition("-")
        if head and tail.isdigit():
            s = head + "." + tail
    return s


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _deco(text: str, base: Style, q: fuzzy.Query | None) -> Row:
    styles = [base] * len(text)
    for m in KW_RE.finditer(text):
        st = Style.PR if PR_KW.match(m.group(0)) else Style.ISSUE
        for i in range(m.start(), m.end()):
            styles[i] = st
    if q:
        for start, end in fuzzy.spans(q, text):
            for i in range(start, min(end, len(text))):
                styles[i] = Style.HIT
    out: Row = []
    for ch, st in zip(text, styles):
        if out and out[-1][1] is st:
            out[-1] = (out[-1][0] + ch, st)
        else:
            out.append((ch, st))
    return out


def _render_transcript(sid: str, path: str, q: fuzzy.Query | None) -> list[Row]:
    head, tail, truncated = _load(path)
    recs = head + tail
    mtime = os.path.getmtime(path)

    custom = ai = branch = worktree = wtpath = relocated = cwd = ""
    effort = agentset = mode = permmode = version = ""
    models: dict[str, int] = {}
    skills: list[str] = []
    prs: list[tuple] = []
    frames: list[tuple[str, str]] = []
    touched: set[str] = set()
    ctx = None
    ts_min = ts_max = None

    for o in recs:
        t = o.get("type")
        if t == "custom-title":
            v = o.get("customTitle")
            if isinstance(v, str) and v.strip():
                custom = v.strip()
        elif t == "ai-title":
            v = o.get("aiTitle")
            if isinstance(v, str) and v.strip():
                ai = v.strip()
        elif t == "agent-setting":
            v = o.get("agentSetting")
            if isinstance(v, str) and v.strip():
                agentset = v.strip()
        elif t == "mode":
            v = o.get("mode")
            if isinstance(v, str) and v.strip():
                mode = v.strip()
        elif t == "permission-mode":
            v = o.get("permissionMode")
            if isinstance(v, str) and v.strip():
                permmode = v.strip()
        elif t == "pr-link":
            n = o.get("prNumber")
            if n is not None and not any(p[0] == n for p in prs):
                prs.append((n, o.get("prRepository") or "", o.get("prUrl") or ""))
        elif t == "frame-link":
            u = o.get("frameUrl") or ""
            if not any(f[1] == u for f in frames):
                frames.append((o.get("title") or o.get("path") or "", u))
        elif t == "worktree-state":
            ws = o.get("worktreeSession") or {}
            if isinstance(ws, dict):
                wn = ws.get("worktreeName")
                if isinstance(wn, str) and wn.strip():
                    worktree = wn.strip()
                wp = ws.get("worktreePath")
                if isinstance(wp, str) and wp.strip():
                    wtpath = wp.strip()
        elif t == "relocated":
            v = o.get("relocatedCwd")
            if isinstance(v, str) and v.strip():
                relocated = v.strip()
        elif t == "file-history-delta":
            tp = o.get("trackingPath")
            if isinstance(tp, str) and tp:
                touched.add(tp)
        elif t in ("user", "assistant"):
            ts = o.get("timestamp")
            if isinstance(ts, str):
                d = _parse_ts(ts)
                if d is not None:
                    if ts_min is None or d < ts_min:
                        ts_min = d
                    if ts_max is None or d > ts_max:
                        ts_max = d
            for key in ("cwd", "version", "effort", "permissionMode"):
                v = o.get(key)
                if isinstance(v, str) and v.strip():
                    if key == "cwd":
                        cwd = v.strip()
                    elif key == "version":
                        version = v.strip()
                    elif key == "effort":
                        effort = v.strip()
                    else:
                        permmode = v.strip()
            sk = o.get("attributionSkill")
            if isinstance(sk, str) and sk.strip() and sk.strip() not in skills:
                skills.append(sk.strip())
            if t == "assistant":
                m = (o.get("message") or {}).get("model")
                if isinstance(m, str) and m and not m.startswith("<"):
                    models[m] = models.get(m, 0) + 1

    users = 0
    msgs: list[tuple[str | None, object]] = []
    for o in tail:
        t = o.get("type")
        if t not in ("user", "assistant"):
            continue
        gb = o.get("gitBranch")
        if isinstance(gb, str) and gb:
            branch = gb
        if t == "assistant" and not o.get("isSidechain"):
            usage = (o.get("message") or {}).get("usage")
            if isinstance(usage, dict):
                ctx = sum(usage.get(k) or 0 for k in
                          ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        if o.get("isMeta"):
            continue
        body, tools = _text_of(o)
        if not body:
            if t == "assistant" and tools:
                if msgs and msgs[-1][0] is None:
                    msgs[-1] = (None, msgs[-1][1] + tools)
                else:
                    msgs.append((None, tools))
            continue
        if t == "user" and body.startswith("<"):
            continue
        if t == "user" and not o.get("isSidechain"):
            users += 1
        if len(body) > MSG_MAX:
            body = body[:MSG_MAX - 1] + "…"
        msgs.append((t, body))

    title = custom or ai or ""
    if not title:
        for t, body in msgs:
            if t == "user":
                title = str(body)[:80]
                break

    out: list[Row] = [_deco(title or sid[:8], Style.BOLD, q)]

    stats = [f"{'~' if truncated else ''}{users} turns"]
    if ctx is not None:
        stats.append(f"{fmt_ctx(ctx)} ctx")
    dur = fmt_dur(ts_min, ts_max)
    if dur:
        stats.append(dur)
    stats.append(fmt_age(mtime))
    out.append([(" · ".join(stats), Style.DIM)])

    if not any(o.get("type") in ("user", "assistant") for o in recs):
        out.append([("∅ transcript holds no messages — this session cannot be resumed", Style.DIM)])

    js = daemon.read_json(os.path.join(paths.job_dir(sid), "state.json"))
    state = js.get("state") or ""
    btext = ("bg:" + state if state else "bg") if js else ""
    if daemon.is_live(sid):
        btext = f"● {btext}" if btext else "● live"
    if btext:
        out.append([(btext, STATE_STYLE.get(state, Style.NORMAL))])

    def kv(k: str, v) -> None:
        if v is None:
            return
        v = " ".join(str(v).split())
        if not v:
            return
        out.append([(f"{k:<10}", Style.DIM), *_deco(v, Style.DIM, q)])

    kv("session", sid)
    if models:
        kv("model", ", ".join(_short_model(m) for m, _ in
                              sorted(models.items(), key=lambda kvp: -kvp[1])))
    kv("effort", effort)
    kv("agent", agentset)
    kv("mode", mode)
    kv("perm", permmode)
    kv("cwd", cwd)
    kv("branch", branch)
    if worktree or wtpath:
        kv("worktree", " ".join(x for x in (worktree, wtpath) if x))
    kv("wt-branch", js.get("worktreeBranch"))
    kv("relocated", relocated if relocated not in (cwd, wtpath) else "")
    for n, repo, url in prs:
        kv("pr", f"#{n} {repo}".strip())
        kv("", url)
    for ttl, url in frames:
        kv("artifact", ttl)
        kv("", url)
    if touched:
        kv("files", f"{len(touched)} files touched")
    kv("cli", version)
    if skills:
        kv("skills", ", ".join(skills))
    if js:
        kv("state", state)
        kv("intent", js.get("intent"))
        kv("detail", js.get("detail") or ((js.get("output") or {}).get("result")))
        kv("tokens", js.get("tokens"))
        kv("created", js.get("createdAt"))
        kv("updated", js.get("updatedAt"))

    shown = msgs[-MSG_KEEP:]
    if q:
        hits = [m for m in msgs if m[0] is not None and fuzzy.spans(q, str(m[1]))]
        if hits:
            out.append([(f"{len(hits)} matches", Style.DIM)])
            shown = hits[-MSG_KEEP:]
    out.append([])
    for t, body in shown:
        if t is None:
            n = int(body)
            out.append([(f"· {n} tool call{'s' if n != 1 else ''}", Style.DIM)])
        elif t == "user":
            out.append([("▍User", Style.USER), (" ", Style.NORMAL), *_deco(str(body), Style.NORMAL, q)])
        else:
            out.append([("▍Agent", Style.AGENT), (" ", Style.NORMAL), *_deco(str(body), Style.DIM, q)])
        out.append([])
    return out
