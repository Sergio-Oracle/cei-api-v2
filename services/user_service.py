"""
Service Utilisateurs — couche Modèle du MVC.

Logique métier : création, mise à jour, recherche d'utilisateurs.
Les Contrôleurs (routes) appellent ces fonctions au lieu de manipuler
la session SQLAlchemy directement.
"""
from __future__ import annotations
import os
import re
import unicodedata
from datetime import datetime, timezone

from models import User, UserRole, get_session


def normalize_name(name: str) -> str:
    """Convertir un nom en identifiant slug : 'Jean Dupont' → 'jean.dupont'"""
    name = unicodedata.normalize('NFKD', name)
    name = name.encode('ASCII', 'ignore').decode('ASCII')
    name = name.lower().strip()
    name = re.sub(r'[^\w\s-]', '', name)
    name = re.sub(r'[\s]+', '.', name)
    return name


def get_user_by_id(user_id: int) -> User | None:
    with get_session() as session:
        return session.query(User).filter_by(id=user_id).first()


def get_user_by_email(email: str) -> User | None:
    with get_session() as session:
        return session.query(User).filter_by(email=email.lower().strip()).first()


def list_users(role: str | None = None, formation_id: int | None = None) -> list[dict]:
    with get_session() as session:
        q = session.query(User)
        if role:
            q = q.filter(User.role == UserRole(role))
        if formation_id:
            q = q.filter(User.formation_id == formation_id)
        users = q.order_by(User.full_name).all()
        return [_serialize_user(u) for u in users]


def create_user(email: str, full_name: str, role: str,
                password_hash: str, formation_id: int | None = None) -> User:
    with get_session() as session:
        user = User(
            email=email.lower().strip(),
            full_name=full_name.strip(),
            role=UserRole(role),
            password_hash=password_hash,
            formation_id=formation_id,
            created_at=datetime.now(timezone.utc),
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def update_user(user_id: int, **fields) -> User | None:
    with get_session() as session:
        user = session.query(User).filter_by(id=user_id).first()
        if not user:
            return None
        allowed = {'full_name', 'email', 'role', 'formation_id', 'password_hash'}
        for key, value in fields.items():
            if key in allowed:
                setattr(user, key, value)
        session.commit()
        session.refresh(user)
        return user


def delete_user(user_id: int) -> bool:
    with get_session() as session:
        user = session.query(User).filter_by(id=user_id).first()
        if not user:
            return False
        session.delete(user)
        session.commit()
        return True


def _serialize_user(u: User) -> dict:
    return {
        'id':           u.id,
        'email':        u.email,
        'full_name':    u.full_name,
        'role':         u.role.value if hasattr(u.role, 'value') else u.role,
        'formation_id': u.formation_id,
        'created_at':   u.created_at.isoformat() if u.created_at else None,
    }
