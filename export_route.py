from flask import jsonify, send_file
from auth_paseto import paseto_required, get_current_user_id
from models import User, StudentPaper, UserRole, get_session
from sqlalchemy.orm import joinedload
from datetime import datetime
from io import BytesIO

def register_export_route(app):
    @app.route('/api/papers/<int:paper_id>/export', methods=['GET'])
    @paseto_required
    def export_paper_pdf(paper_id):
        """Exporter une copie corrigée en PDF"""
        try:
            user_id = get_current_user_id()
            session = get_session()
            
            user = session.query(User).filter_by(id=user_id).first()
            paper = session.query(StudentPaper).options(
                joinedload(StudentPaper.subject),
                joinedload(StudentPaper.student)
            ).filter_by(id=paper_id).first()
            
            if not paper:
                session.close()
                return jsonify({'error': 'Copie non trouvée'}), 404
            
            # Permissions
            if user.role == UserRole.STUDENT and paper.student_id != user_id:
                session.close()
                return jsonify({'error': 'Accès non autorisé'}), 403
            
            if user.role == UserRole.PROFESSOR and paper.subject.creator_id != user_id:
                session.close()
                return jsonify({'error': 'Accès non autorisé'}), 403
            
            # Générer PDF avec ReportLab
            from reportlab.lib.pagesizes import A4
            from reportlab.lib import colors
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
            from reportlab.lib.units import inch
            
            buffer = BytesIO()
            doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=0.5*inch, bottomMargin=0.5*inch)
            
            styles = getSampleStyleSheet()
            title_style = ParagraphStyle(
                'CustomTitle',
                parent=styles['Heading1'],
                fontSize=18,
                textColor=colors.HexColor('#2563eb'),
                spaceAfter=20,
                alignment=1
            )
            
            story = []
            story.append(Paragraph("📋 COPIE CORRIGÉE", title_style))
            story.append(Spacer(1, 0.3*inch))
            
            data = [
                ['Étudiant:', paper.student.full_name if paper.student else 'Inconnu'],
                ['Sujet:', paper.subject.title if paper.subject else 'N/A'],
                ['Note:', f"{paper.score}/20" if paper.score else 'N/A'],
                ['Date:', paper.corrected_at.strftime('%d/%m/%Y à %H:%M') if paper.corrected_at else 'N/A']
            ]
            
            table = Table(data, colWidths=[2*inch, 4*inch])
            table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#f1f5f9')),
                ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 11),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                ('TOPPADDING', (0, 0), (-1, -1), 8),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.grey)
            ]))
            
            story.append(table)
            story.append(Spacer(1, 0.4*inch))
            story.append(Paragraph("📝 Correction Détaillée", styles['Heading2']))
            story.append(Spacer(1, 0.2*inch))
            
            correction_lines = paper.grade.split('\n') if paper.grade else ['Pas de correction disponible']
            for line in correction_lines:
                if line.strip():
                    safe_line = line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    try:
                        story.append(Paragraph(safe_line, styles['BodyText']))
                    except:
                        story.append(Paragraph(safe_line[:500], styles['BodyText']))
                    story.append(Spacer(1, 0.1*inch))
            
            story.append(Spacer(1, 0.5*inch))
            footer_style = ParagraphStyle(
                'Footer',
                parent=styles['Normal'],
                fontSize=9,
                textColor=colors.grey,
                alignment=1
            )
            story.append(Paragraph(
                f"Système de Notation Automatisé - Document généré le {datetime.now().strftime('%d/%m/%Y à %H:%M')}",
                footer_style
            ))
            
            doc.build(story)
            buffer.seek(0)
            session.close()
            
            student_name = paper.student.full_name if paper.student else "inconnu"
            filename = f'copie_{paper_id}_{student_name.replace(" ", "_")}.pdf'
            
            return send_file(
                buffer,
                mimetype='application/pdf',
                as_attachment=True,
                download_name=filename
            )
            
        except Exception as e:
            print(f"❌ Erreur export_paper_pdf: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({'error': str(e)}), 500
