# Barres de disponibilité sur 90 jours : la sonde extérieure

Note interne T32 v2.0, § 7 : « des barres de disponibilité sur 90 jours, mesurées pendant les heures du contrat. Elles
demandent une sonde hors du cluster ; sans elle, le chiffre serait faux justement quand le cluster est en panne. »

## Le chemin des mesures

```
probe.yml (GitHub Actions)                              pages.yml (GitHub Actions)
  plan, puis des lots de 4 créneaux (probe-batch.yml)
  measure : sonde les adresses publiques   ──►  branche probe-data  ──►  build --probe-data  ──►  la page
  record : range le lot dès sa fin             probe/AAAA-MM-JJ.jsonl     (toutes les 30 min, et à chaque poussée)
  (cibles : secret PROBE_TARGETS)
```

- La sonde tourne chez GitHub : **hors du cluster et hors du réseau de Yanedo**. Elle voit ce qu'un client voit
  depuis Internet.
- Départs planifiés : du lundi au vendredi, de 06:00 à 16:59 UTC, toutes les 5 minutes, à la minute 2, 7, 12, etc.
  Cela couvre 09:00-17:00 à Zurich, en heure d'hiver comme en heure d'été. GitHub n'en lance qu'une petite partie :
  chaque passage lancé mesure donc tous les créneaux jusqu'à 17:00 à Zurich, par lots (« Passages longs, par lots »).
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
  S'il confirme la panne, il continue de la mesurer jusqu'à la fin du lot.

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

## Passages longs, par lots (correctif du 16/11/2026)

Le 16/11/2026, premier jour ouvrable complet, GitHub n'a lancé que **5 des ~132 départs** planifiés (08:06, 10:14,
11:33, 13:49 et 16:00 UTC). La page affichait « Mesures insuffisantes · mesures : 4 sur 92 ». Ce n'est pas un accident
d'un jour : le dépôt `upptime/upptime`, dont la chaîne `uptime.yml` est planifiée toutes les 5 minutes, reçoit lui
aussi **2 à 7 passages par jour** (100 derniers passages planifiés, du 25/10 au 16/11 : écart médian 5 h 20).
Un passage par créneau ne peut donc pas tenir la couverture.

Depuis le correctif, **chaque passage lancé mesure tous les créneaux jusqu'à la fin des heures du contrat** :

- `probe.yml` : le job `plan` compte les créneaux utiles d'ici 17:00 à Zurich (`probe.py plan`), par lots de
  4 créneaux. Hors des heures du contrat, il ne lance aucun lot (un passage à 06:10 UTC en hiver ne coûte que ce job).
- `probe-batch.yml` : un lot, appelé par `probe.yml` dans une matrice `max-parallel: 1`, donc un lot après l'autre :
  - `measure` (`probe.py loop`) lit les créneaux déjà rangés (branche `probe-data`, sans droit), puis mesure au plus
    4 créneaux : le créneau en cours tout de suite s'il est libre, les suivants à leur 2e minute (comme l'ancien
    cron : 2, 7, 12…). Il ne remesure jamais un créneau déjà rangé ;
  - `record` range le lot dès sa fin : **au plus 20 minutes de mesures perdues** si un passage est arrêté ;
  - `record` refuse aussi une deuxième ligne pour un créneau qui a déjà la sienne (garde finale), et refait tout si
    une autre poussée est passée entre la lecture et la poussée.
- Un passage à la fois (`concurrency: probe`, sans annulation) : un départ créé pendant un passage attend, et GitHub
  ne garde que le plus récent. Les départs suivants ne servent donc que de **relève** : si le passage du matin est
  arrêté, le suivant reprend là où les mesures s'arrêtent.
- Rien n'est mesuré hors des heures du contrat (09:00-17:00 à Zurich, du lundi au vendredi), ni hors de la fenêtre
  de la chaîne (06:00-16:59 UTC). Les jours fériés vaudois sont mesurés (la sonde ne lit pas `calendar.yaml`), puis
  écartés par le calcul.
- **Tout en panne d'un coup** : le lot s'arrête ; `confirm`, sur un autre runner, refait la mesure tout de suite. Sa
  ligne remplace la première. S'il confirme la panne, il finit le lot et la panne qui dure reste rangée ; sinon, une
  nouvelle panne totale vue de lui seul n'est pas rangée, et le lot suivant (un autre runner) reprend. Si `confirm`
  échoue, la ligne du premier runner est rangée, comme avant.
- Format des mesures, calcul des barres et page : **inchangés**. Seul le nombre de lignes change : au plus une par
  créneau, au lieu d'une par passage lancé.

### Couverture attendue (rejeu par `tests/simulation.py`, avec le vrai code de `probe.py`)

| Départs lancés par GitHub | Couverture du jour (créneaux mesurés sur 96) |
|---|---|
| Les 5 du 16/11 (08:06, 10:14, 11:33, 13:49, 16:00) | **95 sur 96 (99 %)** ; avant : 4 sur 96 |
| 3 des 5 du 16/11, le pire des 10 tirages (11:33, 13:49, 16:00) | 54 sur 96 (56 %) |
| 3 départs tirés au hasard parmi les 132 du cron (2 000 jours d'hiver) | moyenne 68 % ; un jour sur quatre reste gris |
| 5 départs tirés au hasard (2 000 jours d'hiver) | moyenne 79 % ; un jour sur douze reste gris |

La couverture ne peut pas remonter avant le premier départ du jour : si GitHub ne lance rien avant midi, le jour reste
gris, quelle que soit la sonde. Le témoin à J+7 le dira.

## Usage de GitHub Actions

Conditions de GitHub (« GitHub Terms for Additional Products and Features », Actions) : Actions ne doit pas servir à
une activité sans rapport avec la production, le test, le déploiement ou la publication du projet du dépôt, ni imposer
à ses serveurs une charge disproportionnée par rapport aux bénéfices (exemples cités : un CDN, une application
serverless). Les runners standard sont gratuits pour un dépôt public ; un job dure au plus 6 heures.

| Par jour ouvrable | Temps des jobs | Minutes comptées par GitHub (arrondi par job) | Machines démarrées |
|---|---|---|---|
| Avant, prévu : 132 passages de 2 jobs d'environ 10 s | ~22 min | 264 | 264 |
| Avant, réel le 16/11 : 5 passages | < 1 min | 10 | 10 |
| Après, départs du 16/11 (rejeu) | ~455 min | ~498 | 52 |
| Après, plafond : GitHub lance les 132 départs | ~472 min | ~545 | 85 |

- **Ce n'est plus un usage modeste** : environ 7 h 30 de runner par jour ouvrable, presque toujours à attendre le
  créneau suivant (le processeur dort).
- Il reste dans les limites écrites : jobs de 31 minutes au plus, gratuits pour un dépôt public, au service direct
  de la page publiée par ce dépôt, avec moins de machines démarrées que prévu au départ.
- Si GitHub jugeait cette charge disproportionnée, la solution est l'option B ci-dessous, sans changer le format.

### Option B (non implémentée) : un service de surveillance

Un service extérieur (UptimeRobot, Better Stack, updown.io…) sonde les mêmes adresses chaque minute, depuis plusieurs
sites, avec confirmation. Une chaîne courte de GitHub (quelques passages par jour suffisent) relit ses résultats par
son API, les convertit en lignes `yanedo-status-probe/1` (un créneau = le pire des contrôles du créneau) et les range
sur `probe-data` avec `record`. Format, calcul et page inchangés. Il faut :
- un compte et un sous-traitant nouveaux (décision de Yannick ; conditions d'usage commercial ; adresses publiques
  seulement) et un abonnement ;
- un jeton d'API, en secret de plus ;
- le critère « disponible » rejoué par le service : réponse JSON (mot-clé attendu), 401/403 attendu, bannière IMAPS et
  SMTPS, SOA avec autorité. Ces contrôles n'existent pas tels quels partout : à vérifier service par service.

## Mise en service (gestes de Yannick, dans l'ordre)

1. **Essai à blanc sur un poste hors du VPN** (partage de connexion d'un téléphone) :
   `python3 -I probe.py measure --targets-file cibles.json`. Chaque service doit sortir `up` ; sinon corriger le
   fichier. C'est le témoin de la forme réelle des réponses.
2. Dans le dépôt de la page (GitHub), Settings › Secrets and variables › Actions : secret de dépôt `PROBE_TARGETS`
   = le contenu du fichier.
3. Poussée par la Master (commits signés de `yanedo/status`) : `publish` pose `probe.yml` à côté de `pages.yml`.
4. GitHub › Actions › `probe` › Run workflow, **pendant les heures du contrat** (en dehors, `plan` ne lance aucun
   lot : c'est voulu). Attendu : `plan` vert, puis `batch (1)`, `batch (2)`… l'un après l'autre, chacun avec
   `measure` vert, `confirm` sauté et `record` vert ; une branche `probe-data` avec `probe/<date>.jsonl`, une ligne
   par créneau. Le journal ne montre que des clés de service.
5. GitHub › Actions › `pages` › Run workflow. La page montre les barres, « Mesures depuis le … » et « Dernière
   mesure : … ».

## Limites connues

- Le planificateur de GitHub retarde des passages et en perd beaucoup (5 sur ~132 le 16/11/2026). Un passage lancé
  mesure jusqu'au soir, mais rien ne mesure avant le premier départ du jour : si GitHub ne lance rien avant midi, le
  jour reste gris. Un créneau perdu est « non mesuré », la couverture du jour le montre.
- Un passage interrompu (annulé, runner perdu) perd son lot en cours, au plus 20 minutes ; le départ suivant reprend.
- Dans un dépôt public, GitHub coupe les chaînes planifiées après 60 jours sans activité. Les mesures sont des
  commits, et chaque version en pousse aussi. Le témoin est la « Dernière mesure » sur la page.
- Les runners de GitHub sont surtout aux États-Unis. Une panne du transit vers la Suisse peut ressembler à une panne
  de Yanedo si les deux témoins répondent quand même.
- Les points sondés prouvent que la passerelle, l'application et sa route répondent. Ils ne prouvent pas que tout
  le parcours d'un client fonctionne (connexion, écriture en base). Un incident déclaré reste la source pour cela :
  c'est pourquoi un jour avec un incident n'est jamais vert.
