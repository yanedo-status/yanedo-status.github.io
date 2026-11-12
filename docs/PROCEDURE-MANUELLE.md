# Mettre la page d'état à jour à la main (cluster arrêté)

À suivre par un administrateur d'Argus quand les chaînes ne tournent plus : cluster arrêté, Gitea injoignable, locaux
coupés. C'est justement le moment où les clients regardent la page (note T32 v2.0, § 15).
Tout se fait **dans un navigateur, même depuis un téléphone**. Il ne faut ni Python, ni VPN, ni accès au cluster.

## Ce qu'il faut

- **Votre propre compte GitHub**, avec sa 2FA, membre de l'organisation `yanedo-status` et autorisé en écriture sur le
  dépôt de la page. Chaque mise à jour porte ainsi le nom de la personne qui l'a faite.
- Jamais le compte `yanedo-status-bot` : il ne sert qu'à la chaîne de publication (les conditions de GitHub réservent un
  compte machine aux tâches automatiques). Ses codes de récupération, dans le coffre 1Password des administrateurs
  d'Argus, ne servent qu'à le récupérer.
- Le dépôt public de la page, par exemple `yanedo-status/yanedo-status.github.io`. On ne touche **que** `content/events/`.

## Publier un incident, ou sa mise à jour

1. Ouvrir `content/modeles/` et choisir le modèle :
   - `incident.json` pour un nouvel incident ou une mise à jour ;
   - `incident-resolu.json` pour la résolution ;
   - `maintenance.json` pour une maintenance.

   Ouvrir le fichier, puis copier son contenu (bouton « Copy raw file »).
2. Ouvrir `content/events/`, puis « Add file » › « Create new file ».
   - Nom : `<id>--<n>.json`. Exemple : `2026-11-24-panne-documents--1.json`, puis `--2.json` pour la mise à jour suivante.
   - Coller le modèle, puis modifier :
     - `id` : le même pour toutes les mises à jour d'un même incident. Minuscules, chiffres et tirets.
     - `status` : `reported` (problème signalé), `investigating` (cause recherchée), `fixing` (correctif en cours),
       `monitoring` (surveillance) ou `resolved` (résolu).
     - `at` : l'heure de **maintenant**, au format `2026-11-24T09:58:00+01:00`. Le décalage vaut `+01:00` en hiver et
       `+02:00` en été. Cette heure devient la « Dernière mise à jour » de la page.
     - `modules` : les clés de `content/services.yaml`, par exemple `["dms"]` pour Documents, ou `["all"]` pour tout.
     - `severity` : `partial` (perturbation partielle) ou `major` (panne).
     - `effect` : l'effet concret, vu du client, en **trois langues** (fr, de, en).
     - `text` : le message de cette mise à jour, en trois langues.
     - `next_update_by` : l'heure de la prochaine mise à jour promise. **Obligatoire tant que ce n'est pas résolu** : le
       silence inquiète plus qu'une mauvaise nouvelle.
3. « Commit changes… » › « Commit directly to the main branch ».
4. Ouvrir l'onglet **Actions**. La chaîne `pages` doit passer au vert en une minute environ.
   - Si elle est rouge : ouvrir le run et lire les lignes `REFUS :`. Elles disent quel champ corriger.
   - Corriger le fichier (icône crayon), puis recommencer à l'étape 3.
5. Ouvrir la page et vérifier deux choses : le bandeau, et l'heure de « Dernière mise à jour ».
6. Tenir la promesse : publier la mise à jour suivante **avant** l'heure annoncée dans `next_update_by`.

Le courriel aux contacts des organisations reste à part : il part de Yanedo, jamais de l'hébergeur.

## Ce qui est refusé (et pourquoi)

La page est publique. La construction refuse, et la page reste inchangée, si un texte contient :
- une adresse en `.internal` ;
- une adresse IP ;
- une adresse e-mail ;
- un numéro de téléphone ;
- une adresse web ;
- un identifiant CVE ;
- un nom technique (`xxx-api`, `xxx-web`, `yanedo-xxx`).

Elle refuse aussi :
- un champ inconnu ;
- une langue manquante ;
- une heure sans décalage ou dans le futur.

**N'écrire ni nom de client, ni nom de personne, ni détail d'une faille non corrigée** : la garde ne peut pas tout voir.

## Retour à la normale (cluster rétabli)

Tant que les fichiers ajoutés à la main ne sont pas reportés dans `yanedo/status`, la chaîne `publish` de Gitea
**refuse** de publier. Elle ne les écrase jamais, et elle nomme les fichiers en cause.

1. Pour chaque fichier ajouté chez l'hébergeur : le télécharger tel quel (« Download raw file »).
2. Le déposer **à l'identique**, sous le même nom, dans `content/events/` de `yanedo/status`, par un commit signé ou une
   demande d'ajout.
3. La chaîne `publish` repasse au vert : la page est de nouveau tenue par Gitea.

## Essai obligatoire (une fois, avant l'ouverture, porte P4 de la T32)

Publier à la main une maintenance d'essai, sans effet pour les clients. Elle reste 30 jours dans l'historique.

```json
{"schema": "yanedo-status-event/1", "id": "essai-page-d-etat", "kind": "maintenance", "status": "completed",
 "at": "<maintenant>", "start": "<maintenant moins 5 minutes>", "end": "<maintenant>", "modules": ["all"],
 "impact": "none",
 "title": {"fr": "Essai de la page d'état", "de": "Test der Statusseite", "en": "Status page test"},
 "text": {"fr": "Essai de la mise à jour de cette page. Aucun effet sur les services.",
          "de": "Test der Aktualisierung dieser Seite. Keine Auswirkung auf die Dienste.",
          "en": "Test of updating this page. No effect on the services."}}
```

L'essai est réussi quand les cinq conditions sont réunies :
- la chaîne `pages` est verte ;
- la page montre l'essai dans « Derniers événements » ;
- la « Dernière mise à jour » a changé ;
- le flux RSS contient l'entrée ;
- après le « Retour à la normale », la chaîne `publish` de Gitea refuse d'abord (fichier non reporté), puis passe au
  vert une fois le fichier reporté.
