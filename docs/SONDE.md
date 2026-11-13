# Barres de disponibilité sur 90 jours : la sonde extérieure

Note interne T32 v2.0, § 7 : « des barres de disponibilité sur 90 jours, mesurées pendant les heures du contrat. Elles
demandent une sonde hors du cluster ; sans elle, le chiffre serait faux justement quand le cluster est en panne. »

## Le chemin des mesures

```
probe.yml (GitHub Actions, toutes les 5 min)          pages.yml (GitHub Actions)
  measure : sonde les adresses publiques   ──►  branche probe-data  ──►  build --probe-data  ──►  la page
  (cibles : secret PROBE_TARGETS)              probe/AAAA-MM-JJ.jsonl     (toutes les 30 min, et à chaque poussée)
```

- La sonde tourne chez GitHub : **hors du cluster et hors du réseau de Yanedo**. Elle voit ce qu'un client voit
  depuis Internet.
- Planification : du lundi au vendredi, de 06:00 à 16:59 UTC, toutes les 5 minutes, à la minute 2, 7, 12, etc. Cela
  couvre 09:00-17:00 à Zurich, en heure d'hiver comme en heure d'été.
- Les mesures sont publiques, sur la branche `probe-data` du dépôt de la page. Elles ne contiennent que la clé du
  service, `up` ou `down`, et une raison générique (`http-503`, `timeout`, `not-json`…).
- `publish-git` (depuis `yanedo/status`) ne touche que `main` : il ne lit ni n'écrit jamais `probe-data`.

## Ce qui est sondé, et quand un service est « disponible »

Les cibles **ne sont pas dans ce dépôt** : elles sont dans le secret `PROBE_TARGETS` du dépôt de la page.
`probe.py` les masque dans les journaux publics de GitHub. Aucune mesure ni aucun message ne cite une adresse.

| Type | Disponible si |
|---|---|
| `https` | le code HTTP est dans `expect` (200 par défaut ; jamais un 5xx), avec un certificat valide et sans suivre de redirection. Avec `json: true`, la réponse doit en plus être du JSON (`Content-Type` et premier caractère). **Une route inconnue peut renvoyer la page HTML de l'application avec un 200** : sans ce contrôle, un module arrêté paraîtrait disponible. `expect: [401, 403]` sert pour un module qui n'a pas de point anonyme : le refus vient alors de l'application elle-même (la passerelle seule répondrait 200 ou 503) |
| `tls` / `tcp` | la connexion (TLS vérifiée) s'ouvre et la bannière commence par `banner` (`* OK`, `220`…) |
| `dns` | le serveur répond **avec autorité** au SOA de la zone (requête sans récursion) |
| `any` | l'une au moins de ses vérifications passe (deux serveurs DNS, par exemple) |

- Un service est `up` quand toutes ses vérifications passent, ainsi que celles de `common`, qui valent pour tous.
- **Lent n'est pas en panne** : le contrat d'abonnement dit qu'« un ralentissement de performance n'est pas assimilé
  à une absence » de Disponibilité. Une réponse arrivée, même lente, est donc `up`. Seul un silence de **30 s** sur
  une opération réseau (connexion, envoi, réception) vaut « ne répond pas » (`timeout`). Les témoins gardent 10 s :
  ils ne mesurent que le réseau de la sonde.
- Un échec est refait **20 s plus tard** ; le service n'est `down` que si les deux essais échouent.
- **Témoin positif** : avant de sonder, la sonde interroge deux sites hors de Yanedo. Si aucun ne répond, c'est le
  réseau de la sonde qui est en panne. La mesure est alors rangée avec `witness: false`, et elle ne compte pas.
- **Tout en panne d'un coup** : un deuxième runner, donc en principe une autre adresse, refait la mesure avant
  qu'elle soit rangée. Une liste de blocage qui viserait l'adresse d'un runner ne passe pas pour une panne de Yanedo.

### Format de `PROBE_TARGETS` (exemple fictif)

```json
{
  "schema": "yanedo-status-targets/1",
  "common": [{"type": "https", "url": "https://exemple.invalid/point-commun", "json": true}],
  "services": {
    "ventes": [{"type": "https", "url": "https://exemple.invalid/point-anonyme-a", "json": true}],
    "comptabilite": [{"type": "https", "url": "https://exemple.invalid/point-protege-b", "expect": [401, 403]}],
    "mail": [{"type": "tls", "host": "mail.exemple.invalid", "port": 993, "banner": "* OK"}],
    "dns": [{"type": "any", "checks": [{"type": "dns", "server": "ns1.exemple.invalid", "zone": "exemple.invalid"},
                                       {"type": "dns", "server": "ns2.exemple.invalid", "zone": "exemple.invalid"}]}]
  }
}
```

- Les clés sont celles de `content/services.yaml`. Une clé inconnue, un `http://`, un identifiant dans l'adresse, un
  `expect` en 5xx ou un champ inconnu : tout est **refusé** (code 2, passage rouge). Le message ne cite jamais la valeur.
- Un service absent du secret n'est pas mesuré : ses barres restent « pas encore mesuré ».

## Le calcul (statusgen.py, `Availability`)

- Créneaux de 5 minutes, de 09:00 à 17:00 à Zurich, du lundi au vendredi, hors jours non ouvrables
  (`content/calendar.yaml`, copie de `calendar.yaml` du dépôt `releases`). Les heures étendues 07:00-09:00 et
  17:00-18:00 ne comptent pas : seul le contrat compte.
- Un créneau vaut « disponible » si toutes ses mesures le sont (la pire l'emporte). Un créneau **sans mesure**
  (passage retardé ou perdu par GitHub) ne compte ni pour ni contre.
- Pourcentage du jour = créneaux disponibles / créneaux mesurés. Moins d'un créneau mesuré sur deux : « mesures
  insuffisantes » (gris), **sauf si un échec a été mesuré** ; un échec n'est jamais caché par le gris.
- **Hors du calcul** : une Maintenance Planifiée au sens du contrat d'abonnement, « toute interruption de service
  annoncée au Client au moins cinq (5) Jours Ouvrables à l'avance et réalisée dans la fenêtre annoncée » :
  - le préavis compte les Jours Ouvrables entiers (ni samedi, ni dimanche, ni jour férié vaudois) entre l'annonce et
    le début : le jour de l'annonce ne compte pas, et un jour ne compte que si ses heures ouvrables (jusqu'à 17:00)
    sont finies avant le début. Annoncée le vendredi, une maintenance du lundi de la semaine d'après à 19:00 a ses
    5 jours ; à 06:00, elle n'en a que 4 ;
  - la fenêtre annoncée est celle de la dernière annonce qui avait ce préavis et annonçait une interruption ;
  - seule son intersection avec la fenêtre réalisée (dernière mise à jour, en cours ou terminée) sort du calcul. Un
    dépassement, un report annoncé trop tard, une annulation ou une maintenance déclarée après coup comptent comme
    indisponibilité.
- **Incidents** : ceux de la page (même service, ou tous les services) sont listés dans l'info-bulle du jour. Pendant
  les heures du contrat, ils empêchent le vert : au moins « brèves interruptions » pour une perturbation, au moins
  « interruptions » pour une panne.
- Couleur : 100 % vert ; de 95 à 99,9 % jaune ; de 75 à 94,9 % orange ; moins de 75 % rouge. Couleurs d'état fixes,
  toujours doublées d'un texte : info-bulle, légende, pourcentage par période et tableau des jours sous 100 %.
- **Par mois** : un tableau replié donne, par service, les 3 derniers mois complets et le mois en cours, avec le
  même calcul (il peut remonter avant les 90 jours des barres). « — » : rien de mesuré ; « mesures partielles » :
  moins d'un créneau sur deux mesuré. Volontairement **sans objectif ni crédit** : la page sert tous les clients et
  mesure des modules ; la comparaison au contrat appartient au rapport mensuel de chaque client.
- Arrondi vers le bas : 99,99 % ne s'affiche jamais 100 %.
- Une ligne de mesure illisible est ignorée et comptée (`build` l'affiche) ; elle ne bloque jamais la publication
  d'un incident.

## Mise en service (gestes de Yannick, dans l'ordre)

1. **Essai à blanc sur un poste hors du VPN** (partage de connexion d'un téléphone) :
   `python3 -I probe.py measure --targets-file cibles.json`. Chaque service doit sortir `up` ; sinon corriger le
   fichier. C'est le témoin de la forme réelle des réponses.
2. Dans le dépôt de la page (GitHub), Settings › Secrets and variables › Actions : secret de dépôt `PROBE_TARGETS`
   = le contenu du fichier.
3. Poussée par la Master (commits signés de `yanedo/status`) : `publish` pose `probe.yml` à côté de `pages.yml`.
4. GitHub › Actions › `probe` › Run workflow. Attendu : trois jobs, `confirm` sauté, `record` vert, une branche
   `probe-data` avec `probe/<date>.jsonl`. Le journal ne montre que des clés de service.
5. GitHub › Actions › `pages` › Run workflow. La page montre les barres, « Mesures depuis le … » et « Dernière
   mesure : … ».

## Limites connues

- Le planificateur de GitHub retarde des passages, surtout aux heures pleines, et en perd parfois. On l'évite à la
  minute 0. Le créneau perdu est « non mesuré », la couverture du jour le montre. Le chiffre est un échantillon, pas
  une mesure continue.
- Dans un dépôt public, GitHub coupe les chaînes planifiées après 60 jours sans activité. Les mesures sont des
  commits, et chaque version en pousse aussi. Le témoin est la « Dernière mesure » sur la page.
- Les runners de GitHub sont surtout aux États-Unis. Une panne du transit vers la Suisse peut ressembler à une panne
  de Yanedo si les deux témoins répondent quand même.
- Les points sondés prouvent que la passerelle, l'application et sa route répondent. Ils ne prouvent pas que tout
  le parcours d'un client fonctionne (connexion, écriture en base). Un incident déclaré reste la source pour cela :
  c'est pourquoi un jour avec un incident n'est jamais vert.
