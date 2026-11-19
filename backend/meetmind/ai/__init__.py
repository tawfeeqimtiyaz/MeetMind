"""MeetMind AI: transcription, meeting analysis (LangGraph pipeline) and Q&A."""
from sqlalchemy.orm import sessionmaker

from ..config import Settings
from .pipeline import PipelineDeps


def default_deps(settings: Settings, session_factory: sessionmaker) -> PipelineDeps:
    """The real providers: Groq Whisper for speech, NVIDIA -> Groq -> Ollama for analysis."""
    from .analysis import analyze_meeting
    from .transcription import transcribe_with_groq

    return PipelineDeps(
        session_factory=session_factory,
        settings=settings,
        transcribe=lambda path, hint: transcribe_with_groq(settings, path, hint),
        analyze=lambda ctx, text: analyze_meeting(settings, ctx, text),
    )
