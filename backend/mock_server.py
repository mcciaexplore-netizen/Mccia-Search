"""Mock backend for local UI testing — no real APIs needed."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import random

app = FastAPI(title="MCCIA Search — Mock Mode")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3100"],
    allow_methods=["*"],
    allow_headers=["*"],
)

MOCK_DOCUMENTS = [
    {"filename": "Sampada_1945_July.pdf", "drive_file_id": "mock_001"},
    {"filename": "Sampada_1960_April.pdf", "drive_file_id": "mock_002"},
    {"filename": "MCCIA_Annual_Report_2020.pdf", "drive_file_id": "mock_003"},
    {"filename": "Sampada_1975_March.pdf", "drive_file_id": "mock_004"},
    {"filename": "MCCIA_Membership_Guide.pdf", "drive_file_id": "mock_005"},
]

# Order matters: the first entry with a matching keyword wins, so the specific
# membership entry must come before the history entry, whose "mccia" keyword
# would otherwise catch "MCCIA membership benefits".
MOCK_ANSWERS = [
    {
        "query_keywords": ["membership", "सदस्यत्व", "benefits", "फायदे", "join"],
        "answer": (
            "MCCIA membership offers several benefits to businesses:\n\n"
            "1. **Networking opportunities** with industry leaders\n"
            "2. **Policy advocacy** at state and national level\n"
            "3. **Training and workshops** for skill development\n"
            "4. **Market intelligence** reports and publications\n"
            "5. Access to the **Sampada** monthly journal archive\n\n"
            "Membership is open to businesses of all sizes."
        ),
        "sources": [
            {"filename": "MCCIA_Membership_Guide.pdf", "page_number": 5, "similarity": 0.94},
            {"filename": "MCCIA_Membership_Guide.pdf", "page_number": 8, "similarity": 0.87},
        ],
    },
    {
        "query_keywords": ["mccia", "founded", "history", "established", "स्थापना"],
        "answer": (
            "MCCIA (Mahratta Chamber of Commerce, Industries and Agriculture) "
            "was established in 1934 in Pune. It was founded to represent the "
            "interests of local industries and businesses in the region.\n\n"
            "The chamber has played a pivotal role in the industrial development "
            "of Pune and Maharashtra over the decades."
        ),
        "sources": [
            {"filename": "Sampada_1945_July.pdf", "page_number": 3, "similarity": 0.91},
            {"filename": "MCCIA_Annual_Report_2020.pdf", "page_number": 1, "similarity": 0.78},
        ],
    },
    {
        "query_keywords": ["pune", "industry", "1960", "पुणे", "उद्योग"],
        "answer": (
            "पुण्यातील उद्योग क्षेत्रात १९६० च्या दशकात मोठ्या प्रमाणात "
            "वाढ झाली. त्या काळात मुख्यतः यंत्रसामग्री, कापड आणि रसायन "
            "उद्योग भरभराटीस आले.\n\n"
            "Pune's industrial landscape in the 1960s was dominated by "
            "engineering goods, textiles, and chemical industries. "
            "The Pimpri-Chinchwad area began its transformation into "
            "an industrial hub during this period."
        ),
        "sources": [
            {"filename": "Sampada_1960_April.pdf", "page_number": 12, "similarity": 0.89},
            {"filename": "Sampada_1975_March.pdf", "page_number": 4, "similarity": 0.72},
        ],
    },
]

DEFAULT_ANSWER = {
    "answer": (
        "I found some relevant information in the MCCIA document archive. "
        "The Sampada magazine has been documenting the chamber's activities "
        "and Pune's industrial growth since 1945.\n\n"
        "This is a **mock response** for UI testing. "
        "Connect real API keys to get actual answers from your documents."
    ),
    "sources": [
        {"filename": "Sampada_1945_July.pdf", "page_number": 1, "similarity": 0.65},
    ],
}

NOT_FOUND = {
    "answer": (
        "मला या प्रश्नाचे उत्तर सापडले नाही. "
        "I could not find an answer to your question in the available documents."
    ),
    "sources": [],
    "found": False,
}


@app.get("/health")
def health():
    return {"status": "ok", "service": "mccia-search (mock)"}


@app.post("/chat")
async def chat(req: dict):
    query = req.get("query", "").lower()

    # Simulate processing delay
    await asyncio.sleep(1.2)

    # Return not found for very short queries
    if len(query.strip()) < 3:
        return NOT_FOUND

    # Match against mock answers by keyword
    for mock in MOCK_ANSWERS:
        if any(kw in query for kw in mock["query_keywords"]):
            return {"answer": mock["answer"], "sources": mock["sources"], "found": True}

    # Default answer for anything else
    return {"answer": DEFAULT_ANSWER["answer"], "sources": DEFAULT_ANSWER["sources"], "found": True}


@app.get("/ingest/status")
def ingest_status():
    return {
        "total": len(MOCK_DOCUMENTS),
        "completed": 4,
        "failed": 0,
        "pending": 1,
    }


@app.post("/ingest/sync")
def ingest_sync():
    return {"status": "started", "message": "Sync started in background"}
