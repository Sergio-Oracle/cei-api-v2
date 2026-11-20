#!/bin/bash
# À lancer AVANT tout déploiement de l'API en production : les contrôles qui protègent les notes et l'accès aux examens.
# Refuse (code ≠ 0) si l'un échoue. Usage : cd cei-api-v2 && scripts/verifier_avant_deploiement.sh
cd "$(dirname "$0")/.."
PY=.venv-base/bin/python
ko=0
for t in test_enonces test_corpus_reel test_docx_structure test_phonecam_et_code_affiche test_rubric_sync test_exam_preflight test_appel_surveillant test_messagerie_personnel test_inscription_publique test_extraction_pdf_repli test_limites_etudiants test_association_corrige test_pause_plafonnee test_risk_score test_tech_test test_reprise_sans_code test_affectation_et_liste test_equilibrage_surveillants test_audit_20nov test_migrations_sans_verrou test_charge_tableau_de_bord test_portee_surveillant test_sante_examen test_correction_qcm test_correction_parallele test_bareme_correction test_acces_examen test_securite_acces test_parseur_sujets test_exam_settings test_enregistrement test_renouvellement_session test_robustesse_salle test_alerte_seuil test_notifications_professeur test_notifications_batch test_participants_reels test_total_points test_archivage_sujets test_generation_qualite test_horloge_examen test_rattrapage test_avis_changement_note test_avis_changement_note_routes test_code_sortie_client test_mise_a_jour_client test_correction_ancree test_robustesse_correction_generation test_charge_correction; do
  if $PY scripts/$t.py > /tmp/verif_$t.log 2>&1; then echo "OK   $t  ($(grep -o '[0-9]*/[0-9]* vérifications réussies' /tmp/verif_$t.log | tail -1))"
  else echo "ÉCHEC $t  → /tmp/verif_$t.log"; tail -5 /tmp/verif_$t.log; ko=1; fi
done
[ $ko -eq 0 ] && echo "== Tout est bon : déploiement autorisé ==" || echo "== DÉPLOIEMENT REFUSÉ =="
exit $ko
