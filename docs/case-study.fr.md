# Étude de cas : collecter plusieurs milliers de vidéos YouTube depuis une seule machine

*[English](case-study.md) · [retour au README](../README.fr.md)*

---

## Le projet

Construire un dataset de parole : partir d'un manifest de plusieurs milliers de vidéos
YouTube, télécharger l'audio, et le publier comme dataset Hugging Face au format
AudioFolder.

Les contraintes n'étaient pas des choix de conception. C'étaient des faits concernant la
machine sur laquelle tout devait tourner :

- Un seul portable Windows.
- Une connexion partagée que YouTube limite au bout d'une heure environ.
- Aucun moyen de laisser tourner sans surveillance la nuit.

Tout ce qui suit découle de ces trois lignes.

---

## 1. L'échec qui ne ressemble pas à un échec

La première version fonctionnelle téléchargeait un lot, l'envoyait, et passait au
suivant. Elle tournait bien. Puis les comptes ont cessé d'être cohérents : une exécution
signalait des centaines de vidéos « disparues » qui, après vérification, étaient
parfaitement visibles dans un navigateur.

YouTube renvoie `Video unavailable` pour une vidéo supprimée. Il renvoie aussi
`Video unavailable` quand il limite votre débit. Les chaînes sont identiques.

La première version traitait cela comme définitif et inscrivait ces identifiants dans une
liste « ne plus jamais essayer ». Chaque minute de limitation supprimait définitivement de
bonnes vidéos du travail, et rien dans l'exécution ne paraissait anormal pendant ce temps.
Le dataset serait simplement sorti incomplet, sans trace de ce qui manquait ni pourquoi.

Le correctif est une classification à trois voies avec une asymétrie délibérée :

```python
if any(marker in lowered for marker in BLOCKED_MARKERS):
    return Outcome.BLOCKED     # l'IP, pas la vidéo — arrêter l'exécution
if any(marker in lowered for marker in GONE_MARKERS):
    return Outcome.GONE        # sans ambiguïté et propre à la vidéo — jamais de réessai
return Outcome.RETRY           # tout le reste, y compris « Video unavailable »
```

Trois règles rendent cela sûr :

- **Seules les formulations sans ambiguïté sont `GONE`.** `Private video`,
  `removed by the uploader`, `members-only`. Chacune nomme une propriété de la vidéo
  que la limitation de débit ne peut pas simuler.
- **Les erreurs inconnues sont `RETRY`.** Un réessai coûte quelques secondes. Un `GONE`
  erroné est définitif.
- **`BLOCKED` est vérifié en premier.** Un message de limitation contenant un vocabulaire
  évoquant la vidéo ne doit jamais être lu comme une vidéo morte, sinon un seul moment de
  limitation fait disparaître un groupe entier.

Un budget de réessais (3 exécutions) empêche les vidéos réellement cassées de boucler
indéfiniment — la soupape qui rend « dans le doute, réessayer » abordable.

C'est le seul module avec une densité de tests inhabituelle, dont un nommé
`test_bare_video_unavailable_is_retry_not_gone`. Il documente une décision qu'un lecteur
futur serait autrement tenté de « corriger ».

> L'écriture des tests a révélé une seconde occurrence du même type de bug : le marqueur
> `"not available in your country"` ne correspondait jamais, car le message réel de
> yt-dlp est *« The uploader has not made this video available in your country »*.
> Chaque vidéo bloquée géographiquement était réessayée trois fois avant d'être
> abandonnée. Le test l'a détecté en quelques secondes ; cela coûtait silencieusement des
> requêtes depuis le début.

---

## 2. Rendre l'interruption bon marché plutôt que rare

L'exécution allait être interrompue — elle ne tournait que le jour, par sessions, sur une
connexion qui finirait par être limitée. L'objectif n'a donc jamais été « ne pas
s'arrêter ». Il était : **s'arrêter doit coûter presque rien, et ne jamais rien
corrompre.**

L'unité de travail est devenue un groupe de N vidéos, et l'ordre des étapes à l'intérieur
d'un groupe constitue tout l'argument de sûreté :

```
reset ─▶ download ─▶ package ─▶ upload ─▶ commit ─▶ record ─▶ delete local
                                                     ▲
                       le SEUL endroit où la progression devient permanente
```

Une vidéo est écrite dans le `metadata.csv` courant **après** la réussite de son commit.
Ce seul choix d'ordonnancement ramène tout plantage à un cas sûr :

| Tué pendant | État sur le disque | État sur le Hub | Exécution suivante |
|---|---|---|---|
| le téléchargement | fichiers partiels | inchangé | refait le groupe |
| l'envoi | fichiers complets | blobs partiels, aucun commit | refait le groupe |
| après le commit | — | fichiers commités | saute le groupe |

Aucun entrelacement ne produit « enregistré mais absent ». « Envoyé mais non enregistré »
est possible et seulement coûteux — le groupe est refait et les mêmes fichiers
s'écrasent eux-mêmes. Cette asymétrie est choisie : le gaspillage est récupérable, la
perte ne l'est pas.

Les groupes commencent aussi par un `reset` explicite — supprimer le dossier, supprimer
les lignes du groupe dans la base d'état. Retélécharger quelques vidéos déjà récupérées
coûte peu. Raisonner sur un dossier à moitié rempli laissé par un plantage, non.

**La taille des lots est le réglage que cela expose.** Des petits groupes donnent des
points de reprise fréquents et davantage d'allers-retours d'envoi ; des grands groupes
donnent moins de commits et davantage de perte à l'interruption. J'ai travaillé en petits
lots tant que le réseau était instable, puis plus grands une fois stabilisé.

---

## 3. Des réglages plus rapides produisaient moins de données

Le levier évident était le parallélisme. C'était aussi un piège.

| workers / délai | observé |
|---|---|
| 8 / 0,5s | débit élevé pendant ~10 minutes, puis une vague d'« unavailable » sur de bonnes vidéos |
| 6 / 1s | pareil, arrivant plus lentement |
| **4 / 3s** | **tenu pendant des heures** |

Au-delà du seuil, YouTube ne refuse pas — il se met à mentir, et ces mensonges entrent
dans le classificateur comme des échecs. La configuration rapide terminait chaque lot plus
tôt et collectait moins d'audio exploitable, tout en fabriquant exactement l'erreur
ambiguë dont parle la section 1.

Les deux problèmes n'en font qu'un : le débit est plafonné par ce que le service *distant*
tolère, et le dépasser dégrade la qualité des données avant de dégrader la vitesse.

---

## 4. Une course à l'armement que je n'avais pas prévue

Environ un tiers de l'effort total est passé à faire fonctionner `yt-dlp` tout court.
Dans l'ordre :

**Les cookies.** Le téléchargement anonyme en masse est bloqué presque immédiatement ; les
requêtes doivent donc être authentifiées. L'évident `--cookies-from-browser chrome` échoue
définitivement sous Windows actuel — le chiffrement lié à l'application de Chrome rend le
magasin de cookies inaccessible. La voie qui marche est un `cookies.txt` Netscape exporté
par une extension de navigateur. Il expire au bout de quelques semaines et doit être
réexporté.

**Le défi JavaScript.** YouTube s'est mis à envoyer un défi JS qu'il faut exécuter pour
dériver une signature. yt-dlp ne peut pas le faire seul ; il lui faut un moteur JS dans le
`PATH`. La solution a été Deno, installé au niveau utilisateur, injecté dans
l'environnement du sous-processus de téléchargement :

```python
def child_env(config):
    env = os.environ.copy()
    if config.js_runtime_bin and config.js_runtime_bin.exists():
        env["PATH"] = str(config.js_runtime_bin) + os.pathsep + env.get("PATH", "")
    return env
```

Injecter dans l'enfant plutôt que dans le parent garde la dépendance visible et locale —
et évite de toucher à l'état global de la machine, leçon déjà apprise à mes dépens
(voir §6).

**L'épinglage des versions.** La version stable de yt-dlp ne gérait pas encore le défi ;
le correctif vivait dans une version de développement, avec `yt-dlp[default]` pour les
backends HTTP supplémentaires et `yt-dlp-ejs` pour le pont JS. Un simple
`pip install yt-dlp` s'installe proprement puis échoue sur chaque téléchargement réel —
d'où le commentaire dans `requirements.txt` expliquant la raison de chaque épingle.

**La leçon générale :** quand une dépendance est adverse — activement modifiée pour
empêcher ce que vous faites — traitez sa version et son environnement comme faisant partie
de votre configuration, et non comme des faits ambiants.

---

## 5. La machine s'est mise en veille

Je suis revenu de déjeuner devant un portable qui s'était suspendu en pleine exécution.

Windows définit « inactif » comme l'absence d'entrée clavier et souris. Un pipeline de
téléchargement, c'est des minutes d'attente réseau sans aucune entrée : une longue
exécution ressemble donc exactement à une machine abandonnée. Le mode de gestion de
l'alimentation l'a endormie au bout de 20 minutes.

La mauvaise solution est de modifier globalement le mode d'alimentation de l'utilisateur.
La bonne est que le processus déclare ce dont il a besoin, aussi longtemps qu'il en a
besoin :

```python
set_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED)
atexit.register(keep_awake, False)   # relâché à la fin de l'exécution
```

Limité au processus, rétabli à la sortie, et signalé dans le panneau de démarrage
(`sleep: blocked while this runs`) pour que l'opérateur voie que la protection est réelle.

Cela ne couvre pas un capot refermé — Windows respecte l'interrupteur de capot quoi que
demande un programme. La documentation le dit clairement plutôt que de laisser
l'utilisateur découvrir la limite comme je l'ai fait.

---

## 6. Deux erreurs à consigner

**J'ai tronqué le `PATH` de la machine.** Utiliser `setx` depuis un shell Git Bash pour
ajouter un dossier a silencieusement coupé le `PATH` utilisateur à 1024 caractères,
emportant plusieurs chaînes d'outils. `setx` n'est pas un moyen sûr de modifier le
`PATH` ; `[Environment]::SetEnvironmentVariable`, qui passe par le registre, l'est. La
leçon de fond est celle qui a façonné le §4 : préférer limiter un changement
d'environnement à un processus enfant plutôt que muter la machine.

**J'ai d'abord construit la mauvaise structure.** J'ai commencé par répartir la sortie
sur de nombreux petits dépôts de dataset, parce que c'était ainsi que le manifest d'entrée
était étiqueté. Le besoin réel était un dataset unique ; les étiquettes étaient
vestigiales. Une journée de travail à la poubelle pour avoir lu la structure des données
au lieu du besoin réel.

---

## Résultats

| | |
|---|---|
| Vidéos dans le manifest | ~9 000 |
| Définitivement indisponibles | bien moins de 1 % |
| Interruptions survécues | nombreuses — aucune perte, aucun doublon |
| Débit tenu | ~4 vidéos/min à 4 workers / 3s |

---

## Ce que je ferais différemment

**Se réconcilier avec le Hub, pas seulement avec l'état local.** « Déjà envoyé » est lu
depuis le `metadata.csv` local. Ce fichier n'est écrit qu'après un commit réussi, il n'est
donc jamais *faux* — mais s'il était supprimé, le pipeline referait un travail déjà
publié. Une option `--reconcile` listant `train/audio/` sur le Hub rendrait le système
récupérable à partir du seul dataset. C'est la première lacune que je comblerais.

**Journaliser des événements, pas de la sortie console.** Le journal fait des mégaoctets
de barres de progression brutes de yt-dlp — quasi illisible et inutilisable pour
l'analyse. Un objet JSON par groupe (compteurs, durée, octets, résultat) aurait fait du
tableau de débit du §3 une requête plutôt qu'un après-midi d'observation.

**Tester le classificateur dès le premier jour.** Il a existé une semaine sans tests, et
c'est exactement la semaine où le bug « Video unavailable » était actif. Les tests ont
pris moins d'une heure à écrire et ont immédiatement trouvé un second bug du même type.

## Ce que je garderais

L'orchestration en un seul fichier avec une frontière de sous-processus. Ce n'est pas à la
mode, mais tous les modes de défaillance tiennent sur un écran de code, et la frontière de
processus a largement justifié sa place. Un framework aurait ajouté de l'abstraction sans
ajouter une seule garantie que l'ordonnancement du §2 ne fournit pas déjà.
