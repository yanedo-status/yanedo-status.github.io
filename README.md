# Mesures de la sonde extérieure

Branche écrite par la chaîne `probe` : une ligne JSON par passage de la sonde (toutes les 5 minutes, du lundi au
vendredi, pendant les heures du contrat et autour), dans `probe/AAAA-MM-JJ.jsonl` (date UTC). Format
`yanedo-status-probe/1` : `at` (UTC), `witness` (le réseau de la sonde fonctionnait), `results` (clé du service →
`up` ou `down`), `reasons` (raison générique d'un `down`). La chaîne `pages` les lit pour les barres de
disponibilité. Ne pas modifier à la main.
