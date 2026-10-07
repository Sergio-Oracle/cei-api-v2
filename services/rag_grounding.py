"""
Contrôles anti-invention de la génération ancrée (moteur RAG).

1. Couverture du thème : quand l'enseignant cible un chapitre ou un thème,
   les passages retrouvés doivent vraiment en traiter. Le score de RAGFlow
   ne suffit pas à le décider (mesuré le 07/10 sur la préprod : « droits de
   l'homme » obtient 0,80 dans un cours de systèmes d'exploitation, et
   « ordonnancement des processus » 0,35) : l'IA lit les passages et juge.
2. Réponses vérifiées : pour le sujet complet, chaque question est comparée
   au passage cité dans le barème ([S3]). Une question sans source, ou dont
   la réponse attendue n'est pas justifiée par le passage, est signalée à
   l'enseignant dans l'aperçu (jamais retirée en silence : il décide).

Les deux contrôles sont « non bloquants en cas de panne » : si l'IA ne
répond pas, la génération continue et le contrôle est indiqué comme non fait.
"""
import json
import re

from services.ai_service import call_ai_simple

_Q_SPLIT = re.compile(r'(?m)^\s*Question\s+(\d{1,3})\b')
_SRC = re.compile(r'\[S(\d+)\]')


def _json(text):
    m = re.search(r'[\[{].*[\]}]', text or '', re.S)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except ValueError:
        return None


def check_coverage(focus: str, passages: list) -> dict:
    """{'covered': bool|None, 'reason': str}. None = contrôle non fait (IA indisponible)."""
    extracts = '\n\n'.join(f"[{p['id']}] ({p['filename']})\n{p['content'][:900]}" for p in passages[:10])
    prompt = f"""Un enseignant veut créer un sujet d'examen sur le thème : « {focus} ».
Voici les passages de ses documents de cours les plus proches de ce thème :

{extracts}

Question : ces passages traitent-ils réellement de ce thème, avec assez de matière pour poser des questions d'examen ?
Un simple mot en commun ne suffit pas. Réponds UNIQUEMENT en JSON :
{{"couvert": true ou false, "raison": "une phrase en français"}}"""
    try:
        data = _json(call_ai_simple(prompt, fast=True))
    except Exception as e:
        print(f"[rag_grounding] couverture non vérifiée : {e}")
        return {'covered': None, 'reason': 'contrôle indisponible'}
    if not isinstance(data, dict) or 'couvert' not in data:
        return {'covered': None, 'reason': 'réponse illisible'}
    return {'covered': bool(data['couvert']), 'reason': str(data.get('raison') or '')[:300]}


def _blocks(text: str) -> dict:
    """{numéro: texte} des blocs « Question N … » d'un sujet ou d'un barème."""
    out, marks = {}, list(_Q_SPLIT.finditer(text or ''))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out[int(m.group(1))] = text[m.start():end].strip()
    return out


def verify_answers(content: str, rubric: str, passages: list) -> dict:
    """Vérifie chaque question du sujet complet contre le passage cité dans
    son barème. Renvoie {'checked': n, 'unsupported': [...], 'status': ...}."""
    by_id = {str(p.get('id')): p for p in passages if p.get('content')}
    questions, answers = _blocks(content), _blocks(rubric)
    unsupported, to_check = [], []
    for num in sorted(answers):
        ans = answers[num]
        cited = [f'S{n}' for n in dict.fromkeys(_SRC.findall(ans)) if f'S{n}' in by_id]
        title = (questions.get(num) or ans).splitlines()[0][:160]
        if not cited:
            unsupported.append({'question': num, 'title': title, 'reason': 'Aucun passage du cours cité dans le barème'})
            continue
        to_check.append({'num': num, 'title': title, 'question': (questions.get(num) or '')[:1200],
                         'answer': ans[:800], 'cited': cited})
    status = 'ok'
    for i in range(0, len(to_check), 12):   # lots de 12 questions par appel
        batch = to_check[i:i + 12]
        used = {pid for q in batch for pid in q['cited']}
        extracts = '\n\n'.join(f"[{pid}] {by_id[pid]['content'][:2500]}" for pid in sorted(used))
        items = '\n\n'.join(f"### Question {q['num']} (passages cités : {', '.join(q['cited'])})\n{q['question']}\n--- Barème ---\n{q['answer']}"
                            for q in batch)
        prompt = f"""Tu vérifies un sujet d'examen. Pour chaque question, dis si la réponse attendue (barème) est
justifiée par le ou les passages cités, et UNIQUEMENT par eux (pas par des connaissances générales).

PASSAGES :
{extracts}

QUESTIONS :
{items}

Réponds UNIQUEMENT en JSON, une entrée par question :
[{{"question": 1, "justifiee": true ou false, "raison": "une phrase en français si false"}}]"""
        try:
            data = _json(call_ai_simple(prompt, fast=True))
        except Exception as e:
            print(f"[rag_grounding] vérification non faite : {e}")
            data = None
        if not isinstance(data, list):
            status = 'partial'
            continue
        verdict = {int(x.get('question', 0)): x for x in data if isinstance(x, dict)}
        for q in batch:
            v = verdict.get(q['num'])
            if v is None:
                status = 'partial'
            elif not v.get('justifiee'):
                unsupported.append({'question': q['num'], 'title': q['title'], 'cited': q['cited'],
                                    'reason': str(v.get('raison') or 'Réponse absente du passage cité')[:300]})
    unsupported.sort(key=lambda x: x['question'])
    return {'checked': len(answers), 'unsupported': unsupported, 'status': status}
