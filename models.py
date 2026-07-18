"""Modèles de base de données - Système de Notation Avancé VERSION COMPLÈTE avec détection de doublons"""
from dotenv import load_dotenv
import os
import json

load_dotenv()

from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Text,
    DateTime, Boolean, ForeignKey, Enum as SQLEnum, UniqueConstraint
)
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Text,
    DateTime, Boolean, ForeignKey, Enum as SQLEnum, UniqueConstraint
)
from datetime import timedelta, timezone, datetime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
import enum

Base = declarative_base()

# Enums
class UserRole(enum.Enum):
    STUDENT = "student"
    PROFESSOR = "professor"
    ADMIN = "admin"
    SURVEILLANT = "surveillant"

class ReclamationStatus(enum.Enum):
    PENDING   = "pending"
    IN_REVIEW = "in_review"
    RESOLVED  = "resolved"
    REJECTED  = "rejected"

# MODÈLES POUR LA MAQUETTE PÉDAGOGIQUE
class Pole(Base):
    """Pôle académique UNCHK (STN, LSHE, SEJA)"""
    __tablename__ = 'poles'

    id = Column(Integer, primary_key=True)
    code = Column(String(50), unique=True, nullable=False)
    name = Column(String(200), nullable=False)
    description = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    formations = relationship('Formation', back_populates='pole')
    niveaux = relationship('Niveau', back_populates='pole', order_by='Niveau.code')

    def to_dict(self):
        return {
            'id': self.id,
            'code': self.code,
            'name': self.name,
            'description': self.description,
            'is_active': self.is_active,
            'formations_count': len(self.formations) if self.formations else 0,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class Niveau(Base):
    """Niveau académique UNCHK (Licence 1, Licence 2, ..., Master 2), rattaché à
    un Pôle — hiérarchie : Pôle → Niveau → Formation → Semestre → UE → EC.
    Le code n'est plus unique globalement (ex: "L1" peut exister sous STN ET
    sous LSHE) : l'unicité est appliquée par (pole_id, code) côté route."""
    __tablename__ = 'niveaux'

    id = Column(Integer, primary_key=True)
    code = Column(String(50), nullable=False)
    name = Column(String(200), nullable=False)
    description = Column(Text)
    pole_id = Column(Integer, ForeignKey('poles.id'), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    pole = relationship('Pole', back_populates='niveaux')
    formations = relationship('Formation', back_populates='niveau')

    def to_dict(self):
        return {
            'id': self.id,
            'code': self.code,
            'name': self.name,
            'description': self.description,
            'pole_id': self.pole_id,
            'pole_code': self.pole.code if self.pole else None,
            'pole_name': self.pole.name if self.pole else None,
            'is_active': self.is_active,
            'formations_count': len(self.formations) if self.formations else 0,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class Formation(Base):
    """Formation/Programme académique (ex: Master Télécommunications)"""
    __tablename__ = 'formations'

    id = Column(Integer, primary_key=True)
    code = Column(String(50), unique=True, nullable=False)
    name = Column(String(200), nullable=False)
    # Conservé pour compatibilité avec tout le code existant qui lit level en
    # texte simple (filtres en cascade, affichage) — synchronisé automatiquement
    # depuis niveau.name dès que niveau_id est renseigné (routes/formations.py).
    level = Column(String(50))
    niveau_id = Column(Integer, ForeignKey('niveaux.id'), nullable=True)
    department = Column(String(100))
    description = Column(Text)
    # Hiérarchie : Pôle → Niveau → Formation. pole_id n'est plus saisi
    # directement — il est dérivé de niveau.pole_id à chaque création/mise à
    # jour (routes/formations.py) et conservé ici en colonne dénormalisée
    # pour que les filtres/regroupements par pôle existants (get_pole_formations,
    # affichage "Formations par pôle") n'aient pas besoin d'un JOIN.
    pole_id = Column(Integer, ForeignKey('poles.id'), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    pole = relationship('Pole', back_populates='formations')
    niveau = relationship('Niveau', back_populates='formations')
    semesters = relationship('Semester', back_populates='formation', cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'code': self.code,
            'name': self.name,
            'level': self.level,
            'niveau_id': self.niveau_id,
            'niveau_code': self.niveau.code if self.niveau else None,
            'niveau_name': self.niveau.name if self.niveau else None,
            'department': self.department,
            'description': self.description,
            'pole_id': self.pole_id,
            'pole_code': self.pole.code if self.pole else None,
            'pole_name': self.pole.name if self.pole else None,
            'is_active': self.is_active,
            'semesters_count': len(self.semesters) if self.semesters else 0,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

class Semester(Base):
    """Semestre d'une formation"""
    __tablename__ = 'semesters'

    id = Column(Integer, primary_key=True)
    formation_id = Column(Integer, ForeignKey('formations.id'), nullable=False)
    number = Column(Integer, nullable=False)
    name = Column(String(100))
    total_credits = Column(Integer, default=30)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    formation = relationship('Formation', back_populates='semesters')
    ues = relationship('UE', back_populates='semester', cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'formation_id': self.formation_id,
            'formation_name': self.formation.name if self.formation else None,
            'number': self.number,
            'name': self.name,
            'total_credits': self.total_credits,
            'ues_count': len(self.ues) if self.ues else 0,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

class UE(Base):
    """Unité d'Enseignement"""
    __tablename__ = 'ues'

    id = Column(Integer, primary_key=True)
    semester_id = Column(Integer, ForeignKey('semesters.id'), nullable=False)
    code = Column(String(50), nullable=False, unique=True)
    name = Column(String(200), nullable=False)
    credits = Column(Integer, default=6)
    ue_type = Column(String(50), default='obligatoire')
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    semester = relationship('Semester', back_populates='ues')
    ecs = relationship('EC', back_populates='ue', cascade='all, delete-orphan')
    enrollments = relationship('StudentUEEnrollment', back_populates='ue', cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'semester_id': self.semester_id,
            'semester_name': self.semester.name if self.semester else None,
            'code': self.code,
            'name': self.name,
            'credits': self.credits,
            'ue_type': self.ue_type or 'obligatoire',
            'ecs_count': len(self.ecs) if self.ecs else 0,
            'students_count': len(self.enrollments) if self.enrollments else 0,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

class EC(Base):
    """Élément Constitutif"""
    __tablename__ = 'ecs'

    id = Column(Integer, primary_key=True)
    ue_id = Column(Integer, ForeignKey('ues.id'), nullable=False)
    code = Column(String(50), nullable=False, unique=True)
    name = Column(String(200), nullable=False)
    cm = Column(Integer, default=0)
    td = Column(Integer, default=0)
    tp = Column(Integer, default=0)
    tpe = Column(Integer, default=0)
    vht = Column(Integer, default=0)
    coefficient = Column(Integer, default=1)
    cc_percentage = Column(Integer, default=40)
    ex_percentage = Column(Integer, default=60)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    ue = relationship('UE', back_populates='ecs')
    subjects = relationship('Subject', back_populates='ec')
    assignments = relationship('ECAssignment', back_populates='ec', cascade='all, delete-orphan')

    def to_dict(self):
        semester = self.ue.semester if self.ue else None
        formation = semester.formation if semester else None
        pole = formation.pole if formation else None
        return {
            'id': self.id,
            'ue_id': self.ue_id,
            'ue_code': self.ue.code if self.ue else None,
            'ue_name': self.ue.name if self.ue else None,
            'code': self.code,
            'name': self.name,
            'cm': self.cm,
            'td': self.td,
            'tp': self.tp,
            'tpe': self.tpe,
            'vht': self.vht,
            'coefficient': self.coefficient,
            'cc_percentage': self.cc_percentage if self.cc_percentage is not None else 40,
            'ex_percentage': self.ex_percentage if self.ex_percentage is not None else 60,
            'is_active': self.is_active,
            'assigned_professor_id': self.assignments[0].professor_id if self.assignments else None,
            'assigned_professors': [a.professor_id for a in self.assignments],
            'pole_id': pole.id if pole else None,
            'pole_code': pole.code if pole else None,
            'pole_name': pole.name if pole else None,
            'formation_id': formation.id if formation else None,
            'formation_name': formation.name if formation else None,
            'formation_level': formation.level if formation else None,
            'semester_id': semester.id if semester else None,
            'semester_number': semester.number if semester else None,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

# Affectation EC à Professeur (plusieurs profs possibles par EC)
class ECAssignment(Base):
    __tablename__ = 'ec_assignments'
    id = Column(Integer, primary_key=True)
    ec_id = Column(Integer, ForeignKey('ecs.id'), nullable=False)
    professor_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    assigned_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint('ec_id', 'professor_id', name='unique_ec_professor'),)

    ec = relationship('EC', back_populates='assignments')
    professor = relationship('User')

# NOUVEAU: Inscription Étudiant à UE
class StudentUEEnrollment(Base):
    __tablename__ = 'student_ue_enrollments'
    id = Column(Integer, primary_key=True)
    student_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    ue_id = Column(Integer, ForeignKey('ues.id'), nullable=False)
    enrolled_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint('student_id', 'ue_id', name='unique_student_ue'),)  # Unicité: Pas de double inscription

    student = relationship('User')
    ue = relationship('UE', back_populates='enrollments')

# MODÈLES UTILISATEURS ET COPIES (avec hash pour doublons)
class User(Base):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    email = Column(String(120), unique=True, nullable=True)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(100), nullable=False)
    role = Column(SQLEnum(UserRole), default=UserRole.STUDENT)
    niveau = Column(String(5), nullable=True)   # L1, L2, L3, M1, M2
    is_active = Column(Boolean, default=True)
    email_verified = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime)
    has_email = Column(Boolean, default=True)
    reset_token = Column(String(64), nullable=True)
    reset_token_expires = Column(DateTime, nullable=True)
    notifications_last_read = Column(DateTime, nullable=True)

    formation_id = Column(Integer, ForeignKey('formations.id'), nullable=True)  # formation principale de l'étudiant

    formation = relationship('Formation')
    created_subjects = relationship('Subject', foreign_keys='Subject.creator_id', back_populates='creator')
    student_papers = relationship('StudentPaper', foreign_keys='StudentPaper.student_id', back_populates='student')
    corrected_papers = relationship('StudentPaper', foreign_keys='StudentPaper.corrected_by_id', back_populates='corrector')
    reclamations = relationship('Reclamation', foreign_keys='Reclamation.student_id', back_populates='student')
    responded_reclamations = relationship('Reclamation', foreign_keys='Reclamation.responded_by_id', back_populates='responder')
    ec_assignments = relationship('ECAssignment', back_populates='professor')  # Nouveau: ECs affectés
    ue_enrollments = relationship('StudentUEEnrollment', back_populates='student')  # Nouveau: UEs inscrites

    def to_dict(self):
        # Hiérarchie Pôle → Niveau → Formation : quand l'étudiant est rattaché à
        # une Formation, son niveau "réel" est celui du Niveau de cette Formation
        # (self.niveau reste le texte libre saisi manuellement en fallback quand
        # aucune formation n'est encore choisie).
        f = self.formation
        return {
            'id': self.id,
            'email': self.email,
            'full_name': self.full_name,
            'role': self.role.value,
            'niveau': (f.niveau.code if f and f.niveau else None) or self.niveau,
            'niveau_name': f.niveau.name if f and f.niveau else None,
            'formation_id': self.formation_id,
            'formation_code': f.code if f else None,
            'formation_name': f.name if f else None,
            'pole_code': f.pole.code if f and f.pole else None,
            'pole_name': f.pole.name if f and f.pole else None,
            'is_active': self.is_active,
            'email_verified': self.email_verified,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_login': self.last_login.isoformat() if self.last_login else None
        }

class Subject(Base):
    """Sujet d'examen"""
    __tablename__ = 'subjects'

    id = Column(Integer, primary_key=True)
    ec_id = Column(Integer, ForeignKey('ecs.id'), nullable=True)
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)
    rubric = Column(Text)
    filename = Column(String(255))
    creator_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    ec = relationship('EC', back_populates='subjects')
    creator = relationship('User', foreign_keys=[creator_id], back_populates='created_subjects')
    papers = relationship('StudentPaper', back_populates='subject', cascade='all, delete-orphan')
    online_exams = relationship('OnlineExam', back_populates='subject', cascade='all, delete-orphan')

    # ✅ FIX: méthode to_dict correctement définie DANS la classe Subject
    def to_dict(self):
        return {
            'id': self.id,
            'ec_id': self.ec_id,
            'ec_code': self.ec.code if self.ec else None,
            'ec_name': self.ec.name if self.ec else None,
            'ue_code': self.ec.ue.code if self.ec and self.ec.ue else None,
            'ue_name': self.ec.ue.name if self.ec and self.ec.ue else None,
            'title': self.title,
            'content': self.content,
            'rubric': self.rubric,
            'filename': self.filename,
            'creator_id': self.creator_id,
            'creator_name': self.creator.full_name if self.creator else None,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class SubjectMedia(Base):
    """Pièce jointe image/audio d'un sujet, référencée dans le texte via un
    marqueur [IMAGE:xxx]/[AUDIO:xxx] à côté de la question concernée. Upload
    possible avant la sauvegarde finale du sujet (subject_id nullable — associé
    après coup via link_key le temps de la composition dans l'écran de preview)."""
    __tablename__ = 'subject_media'

    id = Column(Integer, primary_key=True)
    subject_id = Column(Integer, ForeignKey('subjects.id'), nullable=True)
    link_key = Column(String(64), nullable=True)  # clé temporaire avant sauvegarde du sujet
    media_type = Column(String(10), nullable=False)  # 'image' | 'audio'
    filename = Column(String(255), nullable=False)
    s3_key = Column(String(500), nullable=False)
    uploaded_by_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    subject = relationship('Subject')
    uploaded_by = relationship('User')

    def to_dict(self):
        return {
            'id': self.id,
            'subject_id': self.subject_id,
            'media_type': self.media_type,
            'filename': self.filename,
            's3_key': self.s3_key,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class StudentPaper(Base):
    """Copie d'étudiant - AVEC HASH POUR DÉTECTION DE DOUBLONS"""
    __tablename__ = 'student_papers'

    id = Column(Integer, primary_key=True)
    subject_id = Column(Integer, ForeignKey('subjects.id'), nullable=False)
    student_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    content = Column(Text, nullable=False)
    grade = Column(Text)
    score = Column(Float)
    filename = Column(String(255))
    file_hash = Column(String(64), unique=True) # SHA256 pour détecter doublons
    extracted_student_name = Column(String(200)) # Nom extrait par OCR
    corrected_by_id = Column(Integer, ForeignKey('users.id'))
    corrected_at = Column(DateTime)
    reclamation_window_end = Column(DateTime, default=lambda: datetime.utcnow() + timedelta(days=7))  # Fin de la fenêtre de réclamation (corrected_at + 7 jours)
    email_sent = Column(Boolean, default=False) # Email envoyé?
    created_at = Column(DateTime, default=datetime.utcnow)

    subject = relationship('Subject', back_populates='papers')
    student = relationship('User', foreign_keys=[student_id], back_populates='student_papers')
    corrector = relationship('User', foreign_keys=[corrected_by_id], back_populates='corrected_papers')
    reclamations = relationship('Reclamation', back_populates='paper', cascade='all, delete-orphan')
    history = relationship('CorrectionHistory', back_populates='paper', cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'subject_id': self.subject_id,
            'subject_title': self.subject.title if self.subject else None,
            'student_id': self.student_id,
            'student_name': self.student.full_name if self.student else None,
            'student_email': self.student.email if self.student else None,
            'content': self.content,
            'grade': self.grade,
            'score': self.score,
            'filename': self.filename,
            'extracted_student_name': self.extracted_student_name,
            'corrected_by_id': self.corrected_by_id,
            'corrector_name': self.corrector.full_name if self.corrector else None,
            'corrected_at': self.corrected_at.isoformat() if self.corrected_at else None,
            'reclamation_window_end': self.reclamation_window_end.isoformat() if self.reclamation_window_end else None,  # Nouveau
            'email_sent': self.email_sent,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

class Reclamation(Base):
    __tablename__ = 'reclamations'

    id = Column(Integer, primary_key=True)
    paper_id = Column(Integer, ForeignKey('student_papers.id'), nullable=True)
    attempt_id = Column(Integer, ForeignKey('exam_attempts.id'), nullable=True)
    student_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    reason = Column(Text, nullable=False)
    status = Column(SQLEnum(ReclamationStatus), default=ReclamationStatus.PENDING)
    response = Column(Text)

    ia_decision = Column(Text)
    ia_proposed_status = Column(String(50))
    ia_proposed_score = Column(Float)
    ia_proposed_grade = Column(Text)
    ia_proposed_reason = Column(Text)
    ia_processed_at = Column(DateTime)

    responded_by_id = Column(Integer, ForeignKey('users.id'))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    paper = relationship('StudentPaper', back_populates='reclamations', foreign_keys=[paper_id])
    attempt = relationship('ExamAttempt', foreign_keys=[attempt_id])
    student = relationship('User', foreign_keys=[student_id], back_populates='reclamations')
    responder = relationship('User', foreign_keys=[responded_by_id], back_populates='responded_reclamations')

    def to_dict(self):
        subject_title = None
        attempt_score = None
        attempt_feedback = None
        exam_title = None
        if self.paper and self.paper.subject:
            subject_title = self.paper.subject.title
        elif self.attempt:
            exam = self.attempt.exam if hasattr(self.attempt, 'exam') else None
            subject_title = exam.title if exam else 'Examen en ligne'
            exam_title = exam.title if exam else None
            attempt_score = self.attempt.score
            attempt_feedback = self.attempt.feedback
        return {
            'id': self.id,
            'paper_id': self.paper_id,
            'attempt_id': self.attempt_id,
            'type': 'online_exam' if self.attempt_id else 'paper',
            'student_id': self.student_id,
            'student_name': self.student.full_name if self.student else None,
            'subject_title': subject_title,
            'exam_title': exam_title,
            'attempt_score': attempt_score,
            'attempt_feedback': attempt_feedback,
            'reason': self.reason,
            'status': self.status.value,
            'response': self.response,
            'ia_decision': self.ia_decision,
            'ia_proposed_status': self.ia_proposed_status,
            'ia_proposed_score': self.ia_proposed_score,
            'ia_proposed_grade': self.ia_proposed_grade,
            'ia_proposed_reason': self.ia_proposed_reason,
            'ia_processed_at': self.ia_processed_at.isoformat() if self.ia_processed_at else None,
            'responded_by_id': self.responded_by_id,
            'responder_name': self.responder.full_name if self.responder else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }

class CorrectionHistory(Base):
    __tablename__ = 'correction_history'

    id = Column(Integer, primary_key=True)
    paper_id = Column(Integer, ForeignKey('student_papers.id'), nullable=False)
    corrector_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    old_score = Column(Float)
    new_score = Column(Float)
    old_grade = Column(Text)
    new_grade = Column(Text)
    reason = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    paper = relationship('StudentPaper', back_populates='history')
    corrector = relationship('User')

    def to_dict(self):
        return {
            'id': self.id,
            'paper_id': self.paper_id,
            'corrector_id': self.corrector_id,
            'corrector_name': self.corrector.full_name if self.corrector else None,
            'old_score': self.old_score,
            'new_score': self.new_score,
            'reason': self.reason,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

class ExamStatus(enum.Enum):
    DRAFT = "draft"  # Brouillon
    SCHEDULED = "scheduled"  # Planifié
    ACTIVE = "active"  # En cours
    CLOSED = "closed"  # Terminé

class AttemptStatus(enum.Enum):
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    BANNED = "banned"
    AUTO_SUBMITTED = "auto_submitted"  # Soumission automatique (temps écoulé)

class OnlineExam(Base):
    """Examen en ligne avec surveillance"""
    __tablename__ = 'online_exams'
    
    id = Column(Integer, primary_key=True)
    subject_id = Column(Integer, ForeignKey('subjects.id'), nullable=False)
    title = Column(String(200), nullable=False)
    instructions = Column(Text)
    duration_minutes = Column(Integer, nullable=False)  # Durée en minutes
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    
    # Paramètres de sécurité
    max_tab_switches = Column(Integer, default=2)    # Seuil: changements de fenêtre/onglet
    enable_copy_paste = Column(Boolean, default=False)
    enable_right_click = Column(Boolean, default=False)
    randomize_questions = Column(Boolean, default=False)
    questions_per_page = Column(Integer, default=5)  # Pagination des questions (0 = tout sur une page)

    # Seuils de bannissement supplémentaires
    max_no_face_count = Column(Integer, default=10)  # Seuil: nb fois sans visage (-1=désactivé)
    ban_on_devtools = Column(Boolean, default=True)  # Bannir immédiatement si outils dev détectés
    
    # Correction automatique par IA après soumission (optionnel, configuré par le prof)
    auto_correct = Column(Boolean, default=False)

    # Retour #29 — notes masquées aux étudiants tant que le professeur/admin
    # n'a pas explicitement publié les résultats (après délibération), même
    # symétrie que GradeTranscript.is_published pour les relevés de semestre
    results_published = Column(Boolean, default=False)

    status = Column(SQLEnum(ExamStatus), default=ExamStatus.DRAFT)
    created_by_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    subject = relationship('Subject', back_populates='online_exams')
    creator = relationship('User', foreign_keys=[created_by_id])
    attempts = relationship('ExamAttempt', back_populates='exam', cascade='all, delete-orphan')
    proctors = relationship('ExamProctor', back_populates='exam', cascade='all, delete-orphan')
    
    def to_dict(self):
        return {
            'id': self.id,
            'subject_id': self.subject_id,
            'subject_title': self.subject.title if self.subject else None,
            'title': self.title,
            'instructions': self.instructions,
            'duration_minutes': self.duration_minutes,
            'start_time': (self.start_time.replace(tzinfo=__import__('datetime').timezone.utc).isoformat() if self.start_time.tzinfo is None else self.start_time.isoformat()) if self.start_time else None,
            'end_time': (self.end_time.replace(tzinfo=__import__('datetime').timezone.utc).isoformat() if self.end_time.tzinfo is None else self.end_time.isoformat()) if self.end_time else None,
            'max_tab_switches': self.max_tab_switches,
            'enable_copy_paste': self.enable_copy_paste,
            'enable_right_click': self.enable_right_click,
            'randomize_questions': self.randomize_questions,
            'questions_per_page': self.questions_per_page if self.questions_per_page is not None else 5,
            'max_no_face_count': self.max_no_face_count if self.max_no_face_count is not None else 10,
            'ban_on_devtools': self.ban_on_devtools if self.ban_on_devtools is not None else True,
            'status': self.status.value,
            'auto_correct': self.auto_correct if self.auto_correct is not None else False,
            'results_published': self.results_published or False,
            'creator_name': self.creator.full_name if self.creator else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'is_active': self.status == ExamStatus.ACTIVE,
            'attempts_count': len(self.attempts) if self.attempts else 0
        }

class ExamAttempt(Base):
    """Tentative de composition d'un étudiant"""
    __tablename__ = 'exam_attempts'

    id = Column(Integer, primary_key=True)
    exam_id    = Column(Integer, ForeignKey('online_exams.id'), nullable=False, index=True)
    student_id = Column(Integer, ForeignKey('users.id'),        nullable=False, index=True)

    status = Column(SQLEnum(AttemptStatus), default=AttemptStatus.IN_PROGRESS, index=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    submitted_at = Column(DateTime)
    
    # Surveillance
    tab_switches = Column(Integer, default=0)    # Compteur: violations graves (fenêtre, focus...)
    warnings_count = Column(Integer, default=0)  # Compteur: violations mineures
    no_face_count = Column(Integer, default=0)   # Compteur: détections sans visage
    banned_at = Column(DateTime)
    ban_reason = Column(Text)

    # Proctoring LiveKit
    risk_score = Column(Integer, default=0)  # Score de risque 0-100
    current_egress_id = Column(String(255))  # LiveKit Egress ID actif (null si pas d'enregistrement)

    # Signature pré-examen (attestation d'honneur signée avant le timer)
    pre_exam_signature_data = Column(Text)
    # Métadonnées de la signature pré-examen (JSON: strokes, path_length, duration_ms, signed_at)
    pre_exam_signature_meta = Column(Text)
    # Signature post-examen (confirmation lors de la soumission manuelle)
    signature_data = Column(Text)

    # Temps supplémentaire accordé individuellement par le prof/surveillant
    extra_minutes = Column(Integer, default=0)

    # Réponses (JSON ou texte selon format)
    answers = Column(Text)  # JSON stockant les réponses
    
    # Résultats après correction
    score = Column(Float)
    feedback = Column(Text)
    corrected_at = Column(DateTime)
    corrected_by_id = Column(Integer, ForeignKey('users.id'))

    # Note importée depuis un fichier Excel/CSV (étudiant n'ayant pas composé
    # sur la plateforme — épreuve papier ou autre système). Distingue ces
    # tentatives "fantômes" des compositions réelles dans les listes/exports.
    imported_grade = Column(Boolean, default=False)

    exam = relationship('OnlineExam', back_populates='attempts')
    student = relationship('User', foreign_keys=[student_id])
    corrector = relationship('User', foreign_keys=[corrected_by_id])
    activity_logs = relationship('ExamActivityLog', back_populates='attempt', cascade='all, delete-orphan')
    camera_logs = relationship('CameraLog', back_populates='attempt', cascade='all, delete-orphan')
    proctor_assignment = relationship('ProctorAssignment', back_populates='attempt', uselist=False)
    
    def to_dict(self):
        return {
            'id': self.id,
            'exam_id': self.exam_id,
            'exam_title': self.exam.title if self.exam else None,
            'student_id': self.student_id,
            'student_name': self.student.full_name if self.student else None,
            'status': self.status.value,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'submitted_at': self.submitted_at.isoformat() if self.submitted_at else None,
            'tab_switches': self.tab_switches,
            'warnings_count': self.warnings_count,
            'no_face_count': self.no_face_count or 0,
            'banned_at': self.banned_at.isoformat() if self.banned_at else None,
            'ban_reason': self.ban_reason,
            'risk_score': self.risk_score or 0,
            'answers': self.answers,
            'score': self.score,
            'feedback': self.feedback,
            'corrected_at': self.corrected_at.isoformat() if self.corrected_at else None,
            'corrector_name': self.corrector.full_name if self.corrector else None,
            'signature_data': self.signature_data,
            'extra_minutes': self.extra_minutes or 0,
            'imported_grade': self.imported_grade or False,
        }

class ExamActivityLog(Base):
    """Log d'activité pendant l'examen (surveillance)"""
    __tablename__ = 'exam_activity_logs'
    
    id = Column(Integer, primary_key=True)
    attempt_id = Column(Integer, ForeignKey('exam_attempts.id'), nullable=False, index=True)
    event_type = Column(String(50), nullable=False)
    event_data = Column(Text)
    timestamp  = Column(DateTime, default=datetime.utcnow, index=True)

    attempt = relationship('ExamAttempt', back_populates='activity_logs')
    
    def to_dict(self):
        return {
            'id': self.id,
            'attempt_id': self.attempt_id,
            'event_type': self.event_type,
            'event_data': self.event_data,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None
        }

class GradeTranscript(Base):
    """Relevé de notes d'un étudiant — validation par UE (logique LMD)"""
    __tablename__ = 'grade_transcripts'

    id = Column(Integer, primary_key=True)
    student_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    semester_id = Column(Integer, ForeignKey('semesters.id'), nullable=False)

    # Calculs automatiques
    total_credits = Column(Integer, default=0)
    obtained_credits = Column(Integer, default=0)
    gpa = Column(Float)  # Moyenne générale semestrielle pondérée par crédits UE
    ue_details = Column(Text, nullable=True)  # JSON — détail par UE (logique LMD)

    generated_at = Column(DateTime, default=datetime.utcnow)
    generated_by_id = Column(Integer, ForeignKey('users.id'))
    # #29 — publication contrôlée avant délibération
    is_published = Column(Boolean, default=False)

    student = relationship('User', foreign_keys=[student_id])
    semester = relationship('Semester')
    generator = relationship('User', foreign_keys=[generated_by_id])

    def to_dict(self):
        ue_data = None
        if self.ue_details:
            try:
                ue_data = json.loads(self.ue_details)
            except Exception:
                ue_data = None
        return {
            'id': self.id,
            'student_id': self.student_id,
            'student_name': self.student.full_name if self.student else None,
            'semester_id': self.semester_id,
            'semester_name': self.semester.name if self.semester else None,
            'total_credits': self.total_credits,
            'obtained_credits': self.obtained_credits,
            'gpa': self.gpa,
            'validated': (self.gpa >= 10) if self.gpa is not None else False,
            'ue_details': ue_data,
            'generated_at': self.generated_at.isoformat() if self.generated_at else None
        }

class ExamProctor(Base):
    """Surveillant affecté à un examen par l'enseignant"""
    __tablename__ = 'exam_proctors'

    id = Column(Integer, primary_key=True)
    exam_id = Column(Integer, ForeignKey('online_exams.id'), nullable=False)
    proctor_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    assigned_by_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    assigned_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint('exam_id', 'proctor_id', name='unique_exam_proctor'),)

    exam = relationship('OnlineExam', back_populates='proctors')
    proctor = relationship('User', foreign_keys=[proctor_id])
    assigned_by = relationship('User', foreign_keys=[assigned_by_id])

    def to_dict(self):
        return {
            'id': self.id,
            'exam_id': self.exam_id,
            'proctor_id': self.proctor_id,
            'proctor_name': self.proctor.full_name if self.proctor else None,
            'proctor_email': self.proctor.email if self.proctor else None,
            'assigned_by': self.assigned_by.full_name if self.assigned_by else None,
            'assigned_at': self.assigned_at.isoformat() if self.assigned_at else None,
        }


class ProctorGroup(Base):
    """Groupe nommé de surveillants, rattachable à un ou plusieurs EC. Quand un
    examen est créé/activé pour un EC ayant un groupe rattaché, tous les membres
    du groupe sont automatiquement ajoutés comme ExamProctor sur cet examen."""
    __tablename__ = 'proctor_groups'

    id = Column(Integer, primary_key=True)
    name = Column(String(150), nullable=False)
    created_by_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    created_by = relationship('User', foreign_keys=[created_by_id])
    members = relationship('ProctorGroupMember', back_populates='group', cascade='all, delete-orphan')
    ecs = relationship('ProctorGroupEC', back_populates='group', cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'created_by': self.created_by.full_name if self.created_by else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'members': [m.to_dict() for m in self.members],
            'ec_ids': [ge.ec_id for ge in self.ecs],
        }


class ProctorGroupMember(Base):
    __tablename__ = 'proctor_group_members'

    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey('proctor_groups.id'), nullable=False)
    proctor_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    added_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint('group_id', 'proctor_id', name='unique_group_proctor'),)

    group = relationship('ProctorGroup', back_populates='members')
    proctor = relationship('User')

    def to_dict(self):
        return {
            'id': self.id,
            'proctor_id': self.proctor_id,
            'proctor_name': self.proctor.full_name if self.proctor else None,
            'proctor_email': self.proctor.email if self.proctor else None,
            'proctor_last_login': self.proctor.last_login.isoformat() if self.proctor and self.proctor.last_login else None,
        }


class ProctorGroupEC(Base):
    """Rattachement d'un groupe de surveillants à un EC."""
    __tablename__ = 'proctor_group_ecs'

    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey('proctor_groups.id'), nullable=False)
    ec_id = Column(Integer, ForeignKey('ecs.id'), nullable=False)

    __table_args__ = (UniqueConstraint('group_id', 'ec_id', name='unique_group_ec'),)

    group = relationship('ProctorGroup', back_populates='ecs')
    ec = relationship('EC')


class ProctorAssignment(Base):
    """Affectation d'un étudiant à un surveillant.
    Pré-affectation (avant l'examen) : student_id défini, attempt_id NULL.
    Liaison automatique quand l'étudiant démarre : attempt_id mis à jour."""
    __tablename__ = 'proctor_assignments'

    id = Column(Integer, primary_key=True)
    exam_id = Column(Integer, ForeignKey('online_exams.id'), nullable=False)
    proctor_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    student_id = Column(Integer, ForeignKey('users.id'), nullable=True)
    attempt_id = Column(Integer, ForeignKey('exam_attempts.id'), nullable=True)
    assigned_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint('exam_id', 'student_id', name='unique_exam_student_proctor'),)

    exam = relationship('OnlineExam')
    proctor = relationship('User', foreign_keys=[proctor_id])
    student = relationship('User', foreign_keys=[student_id])
    attempt = relationship('ExamAttempt', back_populates='proctor_assignment', foreign_keys=[attempt_id])

    def to_dict(self):
        return {
            'id': self.id,
            'exam_id': self.exam_id,
            'proctor_id': self.proctor_id,
            'student_id': self.student_id,
            'attempt_id': self.attempt_id,
        }


class TokenBlocklist(Base):
    """Refresh tokens révoqués (déconnexion, rotation, compromission)."""
    __tablename__ = 'token_blocklist'
    id         = Column(Integer, primary_key=True)
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    user_id    = Column(Integer, ForeignKey('users.id'), nullable=False)
    revoked_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime(timezone=True), nullable=False)

    @staticmethod
    def hash(token: str) -> str:
        import hashlib
        return hashlib.sha256(token.encode()).hexdigest()


class CameraLog(Base):
    """Logs de surveillance caméra pendant les examens"""
    __tablename__ = 'camera_logs'

    id = Column(Integer, primary_key=True)
    attempt_id = Column(Integer, ForeignKey('exam_attempts.id', ondelete='CASCADE'), nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)
    face_detected = Column(Boolean)
    faces_count = Column(Integer)
    in_frame = Column(Boolean)
    violation_type = Column(String(50))
    violation_severity = Column(String(20))
    image_filename = Column(String(255))
    image_data = Column(Text)
    confidence_score = Column(Float)
    frame_analysis = Column(Text)
    event_type = Column(String(50))

    attempt = relationship('ExamAttempt', back_populates='camera_logs')

    def to_dict(self):
        from s3_client import get_snapshot_url
        if self.image_filename and (
            self.image_filename.startswith('snapshots/') or self.image_filename.startswith('local:')
        ):
            # 'snapshots/...' = MinIO (URL pré-signée) ; 'local:...' = fallback disque (route locale)
            image_url  = get_snapshot_url(self.image_filename)
            image_data = None
        else:
            image_url  = None
            image_data = self.image_data  # base64 legacy
        return {
            'id':           self.id,
            'attempt_id':   self.attempt_id,
            'timestamp':    self.timestamp.isoformat() if self.timestamp else None,
            'event_type':   self.event_type or self.violation_type,
            'face_detected': self.face_detected,
            'faces_count':  self.faces_count,
            'image_url':    image_url,
            'image_data':   image_data,
        }


# #4 — Banque de questions réutilisables
class QuestionBank(Base):
    """Question sauvegardée dans la banque pour réutilisation"""
    __tablename__ = 'question_bank'

    id = Column(Integer, primary_key=True)
    title = Column(String(300), nullable=False)
    content = Column(Text, nullable=False)       # texte complet de la question
    rubric = Column(Text)                        # barème / correction
    question_type = Column(String(30), default='open')  # open | qcm | vf
    bloom_level = Column(String(50))             # Connaissance, Application, etc.
    ec_id = Column(Integer, ForeignKey('ecs.id'), nullable=True)
    created_by_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    ec = relationship('EC')
    creator = relationship('User', foreign_keys=[created_by_id])

    def to_dict(self):
        ec = self.ec
        ue = ec.ue if ec else None
        semester = ue.semester if ue else None
        formation = semester.formation if semester else None
        pole = formation.pole if formation else None
        return {
            'id': self.id,
            'title': self.title,
            'content': self.content,
            'rubric': self.rubric,
            'question_type': self.question_type,
            'bloom_level': self.bloom_level,
            'ec_id': self.ec_id,
            'ec_code': ec.code if ec else None,
            'ec_name': ec.name if ec else None,
            'ue_id': ue.id if ue else None,
            'ue_code': ue.code if ue else None,
            'ue_name': ue.name if ue else None,
            'semester_id': semester.id if semester else None,
            'semester_number': semester.number if semester else None,
            'formation_id': formation.id if formation else None,
            'formation_name': formation.name if formation else None,
            'formation_level': formation.level if formation else None,
            'pole_id': pole.id if pole else None,
            'pole_code': pole.code if pole else None,
            'pole_name': pole.name if pole else None,
            'created_by': self.creator.full_name if self.creator else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

# Configuration de la base de données
DATABASE_URL = os.getenv('DATABASE_URL')

if not DATABASE_URL:
    raise ValueError(
        "❌ ERREUR: DATABASE_URL non défini dans .env!\n"
        "Créez un fichier .env avec:\n"
        "DATABASE_URL=postgresql://exam_user:passer@localhost:5432/exam_grader_db"
    )

print(f"🔗 Connexion à: {DATABASE_URL.split('@')[1] if '@' in DATABASE_URL else DATABASE_URL}")

# pool_size=3, max_overflow=7 → 10 conns max/worker × 9 workers = 90 total
# PostgreSQL max_connections=100 → 10 réservées pour admin/monitoring
engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_recycle=1800,
    pool_size=3,
    max_overflow=7,
    pool_timeout=30,
    connect_args={'options': '-c timezone=UTC'},
)
SessionLocal = sessionmaker(bind=engine)

def get_session():
    return SessionLocal()

def init_db():
    Base.metadata.create_all(engine)
    # Migrations conditionnelles — exécutées uniquement si la colonne/contrainte n'existe pas déjà
    # statement_timeout=2s évite les blocages si la table est verrouillée par l'app active
    from sqlalchemy import text as _text
    _migrations = [
        ("SELECT 1 FROM information_schema.columns WHERE table_name='proctor_assignments' AND column_name='student_id'",
         "ALTER TABLE proctor_assignments ADD COLUMN student_id INTEGER REFERENCES users(id)"),
        # #29 — publication des relevés de notes
        ("SELECT 1 FROM information_schema.columns WHERE table_name='grade_transcripts' AND column_name='is_published'",
         "ALTER TABLE grade_transcripts ADD COLUMN is_published BOOLEAN DEFAULT FALSE"),
        # #7 — image attachée au sujet
        ("SELECT 1 FROM information_schema.columns WHERE table_name='subjects' AND column_name='image_filename'",
         "ALTER TABLE subjects ADD COLUMN image_filename VARCHAR(255)"),
        # formation principale de l'étudiant
        ("SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='formation_id'",
         "ALTER TABLE users ADD COLUMN formation_id INTEGER REFERENCES formations(id) ON DELETE SET NULL"),
    ]
    with engine.connect() as _conn:
        # Timeout court pour éviter le blocage au démarrage si l'app tourne déjà
        try: _conn.execute(_text("SET LOCAL statement_timeout = '2000'"))
        except: pass
        for _check_sql, _alter_sql in _migrations:
            try:
                result = _conn.execute(_text(_check_sql))
                if not result.fetchone():  # Colonne absente → appliquer la migration
                    _conn.execute(_text(_alter_sql))
                    _conn.commit()
            except Exception:
                _conn.rollback()
        # Migrations non-bloquantes (idempotentes)
        _safe_migrations = [
            "ALTER TABLE proctor_assignments ALTER COLUMN attempt_id DROP NOT NULL",
            "ALTER TABLE proctor_assignments DROP CONSTRAINT IF EXISTS unique_attempt_proctor",
        ]
        for _sql in _safe_migrations:
            try:
                _conn.execute(_text(_sql))
                _conn.commit()
            except Exception:
                _conn.rollback()
    print("✅ Base de données initialisée avec examens en ligne et relevés de notes")
