# Événement de la page d'état — schéma `yanedo-status-event/1`

Un fichier JSON (ou YAML) par mise à jour, dans `content/events/`. Toute évolution passe par un schéma `/2`.
Les champs inconnus sont **refusés** : aucun nom de personne, aucun composant ne peut passer par un champ en trop.

| Champ | Obligatoire | Valeurs |
|---|---|---|
| `schema` | oui | `"yanedo-status-event/1"` |
| `id` | oui | `^[a-z0-9][a-z0-9-]{2,63}$`, le même pour toutes les mises à jour d'une maintenance, d'un incident ou d'une version |
| `kind` | oui | `maintenance`, `incident`, `release` |
| `at` | oui | heure de cette mise à jour, RFC 3339 avec décalage ; jamais dans le futur (10 min de tolérance) |
| `status` | oui | maintenance : `scheduled`, `in_progress`, `completed`, `cancelled` · incident : `reported`, `investigating`, `fixing`, `monitoring`, `resolved` · release : `completed`, `rolled_back` |
| `modules` | maintenance, incident ; facultatif pour release | clés de module du shell (`content/services.yaml` : `services` et `internal_keys`), ou `["all"]` pour tous les services publics. Un événement qui ne touche que des modules internes est accepté mais n'apparaît pas sur la page |
| `start`, `end` | maintenance ; `start` facultatif pour release | fenêtre de la maintenance ; début de la promotion |
| `impact` | maintenance | `none`, `degraded`, `unavailable` |
| `severity` | incident | `partial`, `major` |
| `next_update_by` | incident non résolu | heure de la prochaine mise à jour promise (après `at`) |
| `version` | release | `2026-W48` ou `2026-W48.1` ; pour `rolled_back`, la version en place après le retour |
| `interruption` | release, facultatif | `none`, `brief` ; sans mesure, la page n'affiche rien |
| `title` | facultatif | `{fr, de, en}` ; sinon un titre est composé à partir des modules ou de la version |
| `effect` | incident | `{fr, de, en}` : l'effet concret, vu du client |
| `text` | maintenance, incident | `{fr, de, en}` : le message de cette mise à jour |
| `summary` | incident `resolved`, facultatif | `{fr, de, en}` : trois phrases (ce qui s'est passé, ce qu'on a fait, ce qui change) |

Textes : les trois langues, non vides, 500 caractères au plus chacune, et soumis à la garde de contenu public (voir README).

## Ce que la page en déduit

- **Bandeau** : panne (incident `major` ouvert), sinon perturbation partielle (incident `partial` ouvert), sinon maintenance
  en cours (maintenance `in_progress`), sinon tout fonctionne. Les états viennent des événements, **jamais de l'horloge** :
  une maintenance ne passe « en cours » que si un événement le dit.
- **Services** : le pire état parmi les incidents ouverts et les maintenances en cours qui les touchent.
- **Dernière mise à jour** : le plus récent `at`. Une simple reconstruction ne la change pas.
- **Historique** : incidents résolus, maintenances terminées ou annulées, versions, sur les 30 derniers jours.
- **Calendrier** : chaque maintenance devient un `VEVENT` dont `UID` reste stable et dont `SEQUENCE` augmente à chaque
  mise à jour. Les agendas abonnés suivent donc un report ou une annulation.

Exemples : `content/modeles/`.
