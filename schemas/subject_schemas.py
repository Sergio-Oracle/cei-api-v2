"""Pydantic v2 validation schemas for Subject endpoints."""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, field_validator, model_validator


ALLOWED_EXTENSIONS = {'pdf', 'docx', 'doc', 'txt'}
ALLOWED_IMAGE_EXTS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}


class SubjectUploadInput(BaseModel):
    title: str
    ec_id: Optional[int] = None
    question_types: str = ''
    rubric_mode: str = 'ai'  # 'ai' (généré par l'IA) | 'manual' (rédigé par le professeur)

    @field_validator('title')
    @classmethod
    def title_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError('Le titre ne peut pas être vide')
        if len(v) > 200:
            raise ValueError('Le titre ne doit pas dépasser 200 caractères')
        return v

    @field_validator('ec_id', mode='before')
    @classmethod
    def coerce_ec_id(cls, v):
        if v in (None, '', 'null', 'undefined'):
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            raise ValueError('ec_id doit être un entier')

    @field_validator('question_types')
    @classmethod
    def clean_question_types(cls, v: str) -> str:
        return v.strip() if v else ''


class SubjectImageInput(BaseModel):
    extension: str

    @field_validator('extension')
    @classmethod
    def allowed_ext(cls, v: str) -> str:
        v = v.lower()
        if v not in ALLOWED_IMAGE_EXTS:
            raise ValueError(f'Format non autorisé. Utilisez : {", ".join(ALLOWED_IMAGE_EXTS)}')
        return v


class SubjectCreateInput(BaseModel):
    """Validation pour la création manuelle d'un sujet (sans fichier)."""
    title: str
    content: str = ''
    rubric: str = ''
    ec_id: Optional[int] = None

    @field_validator('title')
    @classmethod
    def title_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError('Le titre ne peut pas être vide')
        if len(v) > 200:
            raise ValueError('Le titre ne doit pas dépasser 200 caractères')
        return v

    @field_validator('content', 'rubric', mode='before')
    @classmethod
    def clean_text(cls, v) -> str:
        return (v or '').strip()

    @field_validator('ec_id', mode='before')
    @classmethod
    def coerce_ec_id(cls, v):
        if v in (None, '', 'null', 'undefined', 0):
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            raise ValueError('ec_id doit être un entier')


def validate_upload_form(form: dict) -> SubjectUploadInput:
    """Parse + validate the multipart form fields for subject upload."""
    return SubjectUploadInput(
        title=form.get('title', 'Sans titre'),
        ec_id=form.get('ec_id'),
        question_types=form.get('question_types', ''),
        rubric_mode=form.get('rubric_mode', 'ai'),
    )
