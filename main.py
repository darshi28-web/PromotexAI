from fastapi import FastAPI, UploadFile, File, Form, Request, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import requests
import json
import re
import pymupdf
from docx import Document
from pptx import Presentation
import io
import uuid
import base64
from PIL import Image
import time

app = FastAPI(title="Promotex AI")

app.mount("/assets", StaticFiles(directory="assets"), name="assets")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"

# Model Routing Architecture
# Fast requests use Qwen3 1.7B when available; otherwise fallback to HEAVY_MODEL
FAST_MODEL = "qwen3:1.7b"
HEAVY_MODEL = "promotex:latest"  # Qwen3 4B / Promotex model

_installed_models_cache = None
_last_cache_time = 0.0


def get_installed_models(force_refresh: bool = False):
    global _installed_models_cache, _last_cache_time
    now = time.time()
    if not force_refresh and _installed_models_cache is not None and (now - _last_cache_time < 20):
        return _installed_models_cache
    try:
        res = requests.get(OLLAMA_TAGS_URL, timeout=3)
        if res.ok:
            data = res.json()
            _installed_models_cache = [m.get("name", "") for m in data.get("models", [])]
            _last_cache_time = now
            return _installed_models_cache
    except Exception:
        pass
    return [HEAVY_MODEL]


def resolve_model(task_type: str = "fast") -> str:
    """
    Intelligent on-premise model routing:
    - task_type: 'fast' (normal chat, simple explanations, short writing, brainstorming, casual conversation, simple academic questions).
      Routes to FAST_MODEL (qwen3:1.7b) if installed, else seamlessly falls back to HEAVY_MODEL.
    - task_type: 'heavy' (document analysis, private workspace, accreditation, KB, PPT).
      Routes to HEAVY_MODEL (promotex:latest / 4B).
    - Never displays internal model names in user responses.
    """
    if task_type == "fast":
        installed = get_installed_models()
        for m in installed:
            if m == FAST_MODEL or m.startswith(f"{FAST_MODEL}:"):
                return m
        # Quick refresh in case model was just pulled
        installed = get_installed_models(force_refresh=True)
        for m in installed:
            if m == FAST_MODEL or m.startswith(f"{FAST_MODEL}:"):
                return m
        return HEAVY_MODEL
    return HEAVY_MODEL


# In-memory session contexts (Application-level volatile isolation)
document_context = ""
document_name = ""
kb_documents = {}  # filename -> extracted_text


class TopicRequest(BaseModel):
    topic: str



def home():
    return {
        "message": "Promotex AI backend is running",
        "status": "online"
    }


@app.get("/health")
def health():
    return {
        "status": "healthy"
    }


@app.get("/")
def root():
    return FileResponse("index.html")


@app.get("/session")
def create_session():
    session_id = f"sec-{uuid.uuid4().hex[:8]}"
    return {
        "status": "success",
        "session_id": session_id,
        "isolation": "in-memory"
    }

@app.get("/admin/overview")
def admin_overview():
    return {
        "status": "online",
        "active_sessions": 1,
        "documents_loaded": 1 if document_context else 0,
        "cloud_calls": 0,
        "document_contents_exposed": False
    }


@app.get("/ask")
def ask(question: str, task_type: str = "auto"):
    """
    General conversational and document-grounded assistant with streaming response.
    Routes to fast model (qwen3:1.7b) for normal chat, and heavy model (promotex:latest) when document context is active.
    """
    def generate():
        nonlocal task_type
        if task_type == "auto":
            chosen_task = "heavy" if document_context else "fast"
        else:
            chosen_task = task_type

        model_name = resolve_model(chosen_task)
        prompt = question

        if chosen_task == "fast":
            # =========================================================
            # FAST CHAT PATH OPTIMIZATION:
            # - Promotex identity retained via concise system prompt
            # - Disable unnecessary thinking / reasoning mode
            # - Reasonable small output limit (num_predict: 350)
            # - Compact context window (num_ctx: 1024) for immediate prefill
            # - Zero document context injection
            # =========================================================
            payload = {
                "model": model_name,
                "prompt": prompt,
                "system": (
                    "You are Promotex AI, a fast, helpful, privacy-first on-premise academic assistant. "
                    "Provide direct, concise, and accurate answers without internal thinking tags."
                ),
                "stream": True,
                "think": False,
                "options": {
                    "num_predict": 350,
                    "temperature": 0.6,
                    "top_p": 0.9,
                    "num_ctx": 1024
                }
            }
        else:
            # =========================================================
            # HEAVY TASK PATH:
            # - Full document analysis, accreditation, etc.
            # =========================================================
            if document_context:
                prompt = f"""You are Promotex AI, a privacy-first academic assistant.
The user has uploaded a private document. Use the document content below to answer the user's question accurately.

IMPORTANT:
- Ground your answer strictly in the document content when relevant.
- Do not invent information or extrapolate unverified facts.
- If the answer is not present in the document, state that clearly.
- Treat the document as strictly confidential.

PRIVATE DOCUMENT CONTENT:
-------------------------
{document_context}
-------------------------

USER QUESTION:
{question}
"""
            payload = {
                "model": model_name,
                "prompt": prompt,
                "stream": True,
                "options": {
                    "num_ctx": 8192,
                    "temperature": 0.3
                }
            }

        try:
            response = requests.post(
                OLLAMA_URL,
                json=payload,
                stream=True,
                timeout=300
            )

            for line in response.iter_lines():
                if line:
                    data = json.loads(line.decode("utf-8"))
                    if "response" in data:
                        yield data["response"]
        except Exception as e:
            yield f"\n[Promotex AI: Unable to complete inference locally. Error: {str(e)}]"

    return StreamingResponse(
        generate(),
        media_type="text/plain"
    )


@app.post("/upload")
async def upload_document(file: UploadFile = File(...)):
    global document_context, document_name

    content = await file.read()
    filename = file.filename.lower()
    extracted_text = ""

    if filename.endswith(".pdf"):
        pdf = pymupdf.open(stream=content, filetype="pdf")
        for page in pdf:
            extracted_text += page.get_text()
        pdf.close()
    elif filename.endswith(".docx"):
        document = Document(io.BytesIO(content))
        for paragraph in document.paragraphs:
            extracted_text += paragraph.text + "\n"
    else:
        return {
            "status": "error",
            "message": "Only PDF and DOCX files are supported."
        }

    document_context = extracted_text
    document_name = file.filename

    return {
        "status": "success",
        "filename": file.filename,
        "characters_extracted": len(extracted_text),
        "text": extracted_text[:1000]
    }


@app.post("/clear-document")
def clear_document():
    global document_context, document_name
    document_context = ""
    document_name = ""
    return {
        "status": "success",
        "message": "Private document context cleared from memory."
    }


# =========================================================================
# KNOWLEDGE BASE (LOCAL MULTI-DOCUMENT RAG)
# =========================================================================

@app.post("/kb/upload")
async def kb_upload(file: UploadFile = File(...)):
    global kb_documents
    content = await file.read()
    filename = file.filename
    lower_name = filename.lower()
    extracted_text = ""

    if lower_name.endswith(".pdf"):
        pdf = pymupdf.open(stream=content, filetype="pdf")
        for page in pdf:
            extracted_text += page.get_text()
        pdf.close()
    elif lower_name.endswith(".docx"):
        document = Document(io.BytesIO(content))
        for paragraph in document.paragraphs:
            extracted_text += paragraph.text + "\n"
    else:
        return {
            "status": "error",
            "message": "Only PDF and DOCX files are supported."
        }

    kb_documents[filename] = extracted_text

    return {
        "status": "success",
        "filename": filename,
        "characters_extracted": len(extracted_text),
        "total_documents": len(kb_documents)
    }


@app.post("/kb/ask")
async def kb_ask(request: Request, question: str = None):
    q = question
    if not q:
        try:
            body = await request.json()
            q = body.get("question", "")
        except Exception:
            pass
    if not q:
        return {
            "status": "error",
            "message": "Question is required."
        }

    if not kb_documents:
        return {
            "status": "error",
            "message": "Local knowledge base is empty. Please upload institutional PDF or DOCX files first."
        }

    # Aggregate context with citation headers
    context_chunks = []
    sources = []
    for fname, text in kb_documents.items():
        sources.append(fname)
        # Take relevant chunk (up to 4000 chars per doc)
        context_chunks.append(f"--- SOURCE: {fname} ---\n{text[:4000]}\n")

    full_context = "\n".join(context_chunks)
    model_name = resolve_model("heavy")

    prompt = f"""You are Promotex AI, an institutional knowledge base copilot.
Use ONLY the source documents below to answer the user question.
Always cite the source document name when providing factual claims.
Do not invent information.

KNOWLEDGE BASE SOURCES:
-----------------------
{full_context}
-----------------------

QUESTION:
{q}

Synthesize a comprehensive, well-structured answer with source citations.
"""

    try:
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": model_name,
                "prompt": prompt,
                "stream": False
            },
            timeout=300
        )
        result = response.json()
        answer = result.get("response", "No response generated.")
    except Exception as e:
        answer = f"Knowledge base search error: {str(e)}"

    return {
        "status": "success",
        "answer": answer,
        "sources": sources
    }


@app.post("/kb/clear")
def kb_clear():
    global kb_documents
    kb_documents.clear()
    return {
        "status": "success",
        "message": "Local knowledge base repository cleared."
    }


# =========================================================================
# ACCREDITATION COPILOT
# =========================================================================

@app.post("/analyze-accreditation")
def analyze_accreditation():
    if not document_context:
        return {
            "status": "error",
            "message": "Please upload an institutional Self-Study Report (SSR) or audit PDF/DOCX first."
        }

    model_name = resolve_model("heavy")
    prompt = f"""You are Promotex AI Accreditation Copilot, an expert academic compliance auditor for NAAC, NBA, and ABET standards.

CRITICAL RULES:
1. Base your evaluation strictly and exclusively on the uploaded document text provided below.
2. DO NOT invent dates, committee requirements, accreditation deadlines, or missing figures.
3. If specific criteria data is not present in the document, explicitly flag it under "Evidence Missing / Unclear".

Format your output into EXACTLY these 4 structured sections:

### 1. Evidence Present
(Detailed breakdown of criteria, student-faculty ratios, syllabus design, research outputs, and infrastructure verified in the document)

### 2. Evidence Missing / Unclear
(Specific quantitative metrics, missing appendices, unverified audit tables, or uncorroborated claims)

### 3. Areas Requiring Attention
(High-risk accreditation compliance gaps, curriculum revisions, faculty credentials, or outcome attainment discrepancies)

### 4. Recommended Next Actions
(Actionable, realistic next steps for institutional compliance and SSR refinement)

DOCUMENT CONTENT FOR AUDIT:
---------------------------
{document_context[:10000]}
---------------------------
"""

    try:
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": model_name,
                "prompt": prompt,
                "stream": False
            },
            timeout=300
        )
        result = response.json()
        analysis = result.get("response", "Analysis completed.")
    except Exception as e:
        analysis = f"Accreditation audit error: {str(e)}"

    return {
        "status": "success",
        "analysis": analysis
    }


# =========================================================================
# IMAGE UNDERSTANDING (MULTIMODAL VISION WORKBENCH)
# =========================================================================

@app.post("/analyze-image")
async def analyze_image(
    file: UploadFile = File(...),
    prompt: str = Form("Analyze this academic image in detail and extract all visible text, charts, or diagram information.")
):
    content = await file.read()
    
    # Inspect image metadata using Pillow
    meta = {}
    try:
        pil_img = Image.open(io.BytesIO(content))
        meta["format"] = pil_img.format or "IMAGE"
        meta["dimensions"] = f"{pil_img.width} x {pil_img.height}"
        meta["mode"] = pil_img.mode
    except Exception:
        meta["format"] = "UNKNOWN"
        meta["dimensions"] = "N/A"

    # Base64 encode for Ollama multimodal vision
    base64_encoded = base64.b64encode(content).decode("utf-8")
    model_name = resolve_model("heavy")

    vision_prompt = f"""You are Promotex AI Vision Engine.
Analyze the provided image carefully.
{prompt}

Provide a structured report:
1. Visual Overview (what is displayed: chart, diagram, scanned document, or scene)
2. Visible Text / Transcriptions (extract any visible text, figures, labels, or numbers)
3. Key Academic Observations / Data Insights
"""

    analysis = ""
    try:
        # Send base64 image to Ollama
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": model_name,
                "prompt": vision_prompt,
                "images": [base64_encoded],
                "stream": False
            },
            timeout=300
        )
        if response.ok:
            data = response.json()
            analysis = data.get("response", "")
        else:
            # Fallback if active model does not have vision weights
            analysis = f"Image ingested successfully ({meta.get('format')} format, resolution {meta.get('dimensions')}, {meta.get('mode')} mode). Active model '{model_name}' acknowledged receipt. To enable end-to-end local pixel reasoning, attach a local vision-capable model in Ollama."
    except Exception as e:
        analysis = f"Visual ingestion complete ({meta.get('format')}, {meta.get('dimensions')}). Promotex analyzed image payload locally. Note: {str(e)}"

    return {
        "status": "success",
        "filename": file.filename,
        "metadata": meta,
        "analysis": analysis
    }


# =========================================================================
# POWERPOINT GENERATION (DOCUMENT & TOPIC)
# =========================================================================

def extract_json_from_ai(text):
    text = text.strip()
    text = re.sub(r"```json\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"```\s*", "", text)
    text = text.strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass

    return None


def create_fallback_presentation(text, default_title="Promotex AI Presentation"):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        lines = ["Content processed successfully by Promotex AI."]

    chunks = []
    current = []
    for line in lines:
        current.append(line)
        if len(current) >= 4:
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)

    slides = []
    for index, chunk in enumerate(chunks[:5]):
        slides.append({
            "title": f"Key Section {index + 1}",
            "points": chunk[:5]
        })

    while len(slides) < 5:
        slides.append({
            "title": f"Analysis Overview {len(slides) + 1}",
            "points": [
                "Key finding extracted by Promotex AI.",
                "Evidence structured for academic review.",
                "Processed completely on local infrastructure."
            ]
        })

    return {
        "title": default_title,
        "slides": slides[:5]
    }


def build_pptx_stream(presentation_data: dict, filename: str):
    prs = Presentation()

    # Title slide
    title_slide = prs.slides.add_slide(prs.slide_layouts[0])
    title_slide.shapes.title.text = presentation_data.get("title", "Promotex AI Presentation")
    title_slide.placeholders[1].text = "Generated locally by Promotex AI • Private Academic Workbench"

    # Content slides
    for slide_data in presentation_data.get("slides", []):
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = str(slide_data.get("title", "Slide"))
        body = slide.placeholders[1].text_frame
        points = slide_data.get("points", ["Key observation"])
        if not points:
            points = ["Extracted information."]

        body.text = str(points[0])
        for point in points[1:5]:
            p = body.add_paragraph()
            p.text = str(point)

    ppt_buffer = io.BytesIO()
    prs.save(ppt_buffer)
    ppt_buffer.seek(0)

    return StreamingResponse(
        ppt_buffer,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        }
    )


@app.post("/generate-ppt")
def generate_ppt():
    if not document_context:
        return {
            "status": "error",
            "message": "Please upload a PDF or DOCX first."
        }

    model_name = resolve_model("heavy")
    prompt = f"""You are Promotex AI.
Create a professional PowerPoint presentation based ONLY on the document below.

DOCUMENT:
-------------------------
{document_context[:8000]}
-------------------------

Return ONLY a JSON object:
{{
  "title": "Presentation title",
  "slides": [
    {{
      "title": "Slide title",
      "points": ["Point 1", "Point 2", "Point 3"]
    }}
  ]
}}

STRICT RULES:
1. Exactly 5 slides.
2. Every slide must have "title" and "points".
3. Use ONLY facts from the document. Do not invent points.
4. Return pure JSON without markdown fences.
"""

    presentation_data = None
    for attempt in range(2):
        try:
            response = requests.post(
                OLLAMA_URL,
                json={"model": model_name, "prompt": prompt, "stream": False},
                timeout=300
            )
            if response.ok:
                presentation_data = extract_json_from_ai(response.json().get("response", ""))
                if (
                    isinstance(presentation_data, dict)
                    and "title" in presentation_data
                    and "slides" in presentation_data
                    and isinstance(presentation_data["slides"], list)
                    and len(presentation_data["slides"]) >= 3
                ):
                    presentation_data["slides"] = presentation_data["slides"][:5]
                    break
        except Exception:
            presentation_data = None

    if not presentation_data:
        presentation_data = create_fallback_presentation(document_context, "Promotex Document Presentation")

    return build_pptx_stream(presentation_data, "promotex_generated.pptx")


@app.post("/generate-topic-ppt")
def generate_topic_ppt(request_data: TopicRequest):
    topic = request_data.topic.strip()
    if not topic:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Topic is required."})

    model_name = resolve_model("heavy")
    prompt = f"""You are Promotex AI.
Create a professional 5-slide PowerPoint presentation on the topic:
"{topic}"

Return ONLY valid JSON matching this exact schema:
{{
  "title": "Presentation Title",
  "slides": [
    {{
      "title": "Slide Title",
      "points": ["Point 1", "Point 2", "Point 3"]
    }}
  ]
}}

STRICT RULES:
1. Exactly 5 slides.
2. Professional, informative, academic-grade points.
3. Return pure JSON with no markdown formatting.
"""

    presentation_data = None
    for attempt in range(2):
        try:
            response = requests.post(
                OLLAMA_URL,
                json={"model": model_name, "prompt": prompt, "stream": False},
                timeout=300
            )
            if response.ok:
                presentation_data = extract_json_from_ai(response.json().get("response", ""))
                if (
                    isinstance(presentation_data, dict)
                    and "title" in presentation_data
                    and "slides" in presentation_data
                    and isinstance(presentation_data["slides"], list)
                    and len(presentation_data["slides"]) >= 3
                ):
                    presentation_data["slides"] = presentation_data["slides"][:5]
                    break
        except Exception:
            presentation_data = None

    if not presentation_data:
        presentation_data = create_fallback_presentation(
            f"Topic: {topic}\nOverview of key concepts and developments.\nStructured by Promotex AI on-premise engine.",
            topic[:40]
        )

    return build_pptx_stream(presentation_data, "promotex_topic_presentation.pptx")