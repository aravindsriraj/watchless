"""Local web app: paste a YouTube URL + prompt, read the guide, ask the video questions, export.

Run:  uv run uvicorn app:app --reload   then open http://localhost:8000
"""

import io
import re
import shutil
import zipfile

import markdown
from fastapi import BackgroundTasks, FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

import pipeline

app = FastAPI(title="Watchless")
templates = Jinja2Templates(directory="templates")
templates.env.globals.update(ts=pipeline.ts, at=pipeline.at)

STAGES = ["Reading video info and captions", "Watching the video", "Checking quotes against the captions", "Grabbing screenshots"]
DEFAULT_PROMPT = "A detailed step-by-step guide, showing every screen"

# "▶ 1:07" links to the video become timecode chips the page can play in its own player
TIMECODE = re.compile(r'<a href="([^"]*?[?&](?:amp;)?t=(\d+)s)">▶ ([^<]+)</a>')


def md(text, with_toc=False):
    m = markdown.Markdown(extensions=["extra", "sane_lists", "toc"])
    html = TIMECODE.sub(r'<a class="tc" data-t="\2" href="\1">\3</a>', m.convert(text))
    # colour the check marks on screen; guide.md itself stays plain for export
    html = (html.replace("⚠ quote not found in captions", '<span class="warn">⚠ quote not found in captions</span>')
                .replace("✓", '<span class="ok" title="Quote found in the captions">✓</span>'))
    if not with_toc:
        return html
    sections = [t for top in m.toc_tokens for t in ([top] if top["level"] == 2 else top["children"])]
    return html, sections


templates.env.filters["md"] = md


def job_dir(job_id):
    if not re.fullmatch(r"[\w-]+", job_id) or not (pipeline.LIBRARY / job_id / "meta.json").exists():
        raise HTTPException(404, "No such guide")
    return pipeline.LIBRARY / job_id


def summary(job):
    """Meta plus a few counts for the library list."""
    meta = {"id": job.name, "video_id": job.name[:11], **pipeline.read_json(job / "meta.json")}
    guide = pipeline.read_json(job / "guide.json")
    if guide:
        steps = [st for sec in guide["sections"] for st in sec["steps"]]
        meta.update(n_steps=len(steps), n_ok=sum(1 for st in steps if st["verified"]),
                    n_shots=len(list((job / "images").glob("*.jpg"))))
    return meta


@app.get("/", response_class=HTMLResponse)
def index(request: Request, url: str = ""):
    guides = sorted((summary(p.parent) for p in pipeline.LIBRARY.glob("*/meta.json")),
                    key=lambda g: g.get("created", ""), reverse=True)
    return templates.TemplateResponse(request, "index.html", {"guides": guides, "default_prompt": DEFAULT_PROMPT, "prefill_url": url})


@app.post("/new")
def new(background: BackgroundTasks, url: str = Form(...), prompt: str = Form("")):
    try:
        job = pipeline.new_job(url.strip(), prompt.strip() or DEFAULT_PROMPT)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # ponytail: in-process background task; a server restart mid-job leaves it stuck at its last step (Retry from the page)
    background.add_task(pipeline.run, job)
    return RedirectResponse(f"/g/{job.name}/", status_code=303)


@app.get("/g/{job_id}/", response_class=HTMLResponse)
def guide(request: Request, job_id: str):
    job = job_dir(job_id)
    meta = summary(job)
    ctx = {"id": job_id, "meta": meta, "stages": STAGES, "qa": pipeline.read_json(job / "qa.json", [])}
    guide_json = pipeline.read_json(job / "guide.json")
    if (job / "guide.md").exists() and guide_json:
        html, toc = md((job / "guide.md").read_text(), with_toc=True)
        for entry, sec in zip(toc, guide_json["sections"]):
            checked = [st["verified"] for st in sec["steps"] if st["verified"] is not None]
            entry.update(t=sec["start_s"], ok=sum(checked), n=len(checked))
        ctx.update(guide_html=html, toc=toc)
    return templates.TemplateResponse(request, "guide.html", ctx)


@app.get("/g/{job_id}/status")
def status(job_id: str):
    meta = pipeline.read_json(job_dir(job_id) / "meta.json")
    return {"step": meta["step"], "stage": meta.get("stage", 0), "error": meta.get("error")}


@app.get("/g/{job_id}/images/{name}")
def image(job_id: str, name: str):
    path = job_dir(job_id) / "images" / name
    if not re.fullmatch(r"shot_\d+\.jpg", name) or not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


@app.post("/g/{job_id}/ask", response_class=HTMLResponse)
def ask(request: Request, job_id: str, question: str = Form(...)):
    """Returns the rendered answer as an HTML fragment; the page appends it to the thread."""
    # sync def: FastAPI runs it in a worker thread, so a slow Gemini answer doesn't block other pages
    job = job_dir(job_id)
    if not (job / "guide.md").exists():
        raise HTTPException(409, "The guide is still being made. Ask once it's ready.")
    if not question.strip():
        raise HTTPException(400, "Type a question first.")
    try:
        item = pipeline.ask(job, question.strip())
    except Exception as e:
        raise HTTPException(502, f"Gemini couldn't answer this time ({type(e).__name__}). Try asking again.")
    meta = pipeline.read_json(job / "meta.json")
    return templates.TemplateResponse(request, "_qa.html", {"item": item, "meta": meta})


@app.post("/g/{job_id}/retry")
def retry(background: BackgroundTasks, job_id: str):
    old = job_dir(job_id)
    meta = pipeline.read_json(old / "meta.json")
    job = pipeline.new_job(meta["url"], meta["prompt"])
    shutil.rmtree(old)
    background.add_task(pipeline.run, job)
    return RedirectResponse(f"/g/{job.name}/", status_code=303)


@app.post("/g/{job_id}/delete")
def delete(job_id: str):
    shutil.rmtree(job_dir(job_id))
    return RedirectResponse("/", status_code=303)


@app.get("/g/{job_id}/export.zip")
def export(job_id: str):
    job = job_dir(job_id)
    meta = pipeline.read_json(job / "meta.json")
    qa = pipeline.read_json(job / "qa.json", [])
    text = (job / "guide.md").read_text()
    if qa:
        text += "\n\n## Questions & answers\n\n" + pipeline.qa_markdown(qa, meta["url"])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{job_id}/guide.md", text)
        for img in sorted((job / "images").glob("*.jpg")):
            z.write(img, f"{job_id}/images/{img.name}")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{job_id}.zip"'})
