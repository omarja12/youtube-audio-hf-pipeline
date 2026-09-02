# youtube-audio-hf-pipeline

Collecte reprenable et résistante aux interruptions d'audio YouTube vers un dataset
Hugging Face au format AudioFolder — conçue pour des séries de plusieurs milliers de
vidéos depuis une seule machine, sur une connexion qui limite le débit, où l'exécution
**sera** interrompue.

*[English](README.md) · [Étude de cas technique](docs/case-study.fr.md)*

```
╭──────────────────── my-manifest ─────────────────────╮
│ your-name/your-audio-dataset  (private)              │
│ uploaded so far: 2200/9000   this run: 273 group(s)  │
│ cookies: yes                                         │
│ sleep: blocked while this runs                       │
╰──────────────────────────────────────────────────────╯
─────────────── group 1/273  25 videos  2200/9000 uploaded ────────────────
⠹ DOWNLOAD  group 1/273 ████████████████████░░░░  78%  18/25  OK18  0:00:41
OK  group 1/273: +25 audio  ->  2225 in dataset
```

## Le problème

Télécharger dix mille vidéos n'est pas dix mille fois plus difficile que d'en télécharger
une. C'est un problème différent :

- YouTube **limite votre débit**, et quand il le fait, il ment — il renvoie
  « Video unavailable » pour des vidéos parfaitement valides.
- L'exécution **sera** interrompue. Coupure réseau, redémarrage, capot refermé.
- Certaines vidéos sont **réellement supprimées** et doivent être mémorisées comme telles,
  sinon chaque exécution future gaspille des requêtes à le redécouvrir.
- La machine **ne doit pas se mettre en veille**, et une machine laissée seule le fera.

Se tromper sur l'un de ces points ne fait pas planter le programme. Cela produit
silencieusement un dataset plus petit que prévu, et on s'en aperçoit à la fin.

## Fonctionnement

L'unité de travail est un **groupe** de N vidéos :

```
reset ──▶ download ──▶ build AudioFolder ──▶ merge metadata ──▶ upload ──▶ commit ──▶ delete local
          (sous-processus)                                                    │
                                                                              ▼
                                                        metadata.csv courant := fusionné
```

Cette dernière étape est tout le principe. Une vidéo n'entre dans le `metadata.csv`
courant **qu'après la réussite de son commit**, ce qui fait de ce CSV un registre fidèle
de ce qui se trouve réellement sur le Hub. Toute interruption devient alors sans risque :

| Interrompu pendant | Résultat |
|---|---|
| le téléchargement | fichiers locaux jetés, rien d'enregistré, groupe refait |
| l'envoi | commit jamais validé, rien d'enregistré, groupe refait |
| après le commit | enregistré — le groupe est sauté à l'exécution suivante |

Aucun état ne peut produire « enregistré mais absent ». Le coût d'un plantage est borné
par un seul groupe.

## Démarrage rapide

```powershell
git clone https://github.com/<vous>/youtube-audio-hf-pipeline
cd youtube-audio-hf-pipeline

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

Copy-Item config.example.json config.json   # puis renseigner repo_id
$env:HF_TOKEN = "hf_..."                    # token WRITE, jamais commité

.\.venv\Scripts\python.exe -m src.pipeline --status   # ce qu'il reste
.\.venv\Scripts\python.exe -m src.pipeline            # un groupe, puis arrêt
.\.venv\Scripts\python.exe -m src.pipeline --go       # tout
```

Sous macOS ou Linux, utiliser `.venv/bin/python` et `export HF_TOKEN=...` ; tout est
multiplateforme sauf le blocage de la mise en veille.

Le fichier [`examples/sample-manifest.jsonl`](examples/sample-manifest.jsonl) contient
huit vidéos publiques, suffisant pour observer un cycle complet. Pour construire le vôtre
depuis n'importe quelle chaîne :

```powershell
python .\scripts\make_manifest.py --url "https://www.youtube.com/@chaine/videos" --limit 100 --out examples/mine.jsonl
```

### Faire fonctionner les téléchargements

Le téléchargement en masse exige trois choses au-delà du `pip install`, et sans l'une
d'elles tout échoue :

1. **`cookies.txt`** — un fichier de cookies Netscape exporté depuis un navigateur
   connecté. L'accès anonyme en masse est bloqué presque immédiatement. Utiliser une
   extension « Get cookies.txt LOCALLY ». *Ne pas utiliser `--cookies-from-browser` sous
   Windows* — le chiffrement lié à l'application de Chrome l'a cassé définitivement.
2. **Un moteur JavaScript** — YouTube envoie un défi JS que yt-dlp doit exécuter.
   Installer [Deno](https://deno.com) et pointer `js_runtime_bin` vers son dossier `bin`.
3. **`yt-dlp[default]` et `yt-dlp-ejs`** — le paquet `yt-dlp` simple s'installe puis
   échoue sur du trafic réel.

`cookies.txt` est dans `.gitignore` et doit y rester. C'est une session active du compte
qui l'a exporté — utiliser un compte jetable, pas le vôtre.

## Configuration

Tout ce qui est propre à une exécution vit dans `config.json` (ignoré par git ; copier
`config.example.json`) :

| Clé | Signification |
|---|---|
| `repo_id` | dataset cible, `utilisateur/nom` |
| `manifest` | JSONL des vidéos à récupérer |
| `batch` | vidéos par groupe — plus grand = plus rapide, et plus de perte à l'interruption |
| `cookies` | chemin vers `cookies.txt` |
| `js_runtime_bin` | dossier contenant le binaire Deno |
| `download.workers` | téléchargements parallèles (**4 est le plafond**, voir plus bas) |
| `download.sleep_seconds` | délai après chaque vidéo (**3s**) |

**À propos de ces réglages :** ils sont calibrés pour YouTube, pas pour votre machine.
Au-delà d'environ 4 workers / 3s, YouTube se met à renvoyer de faux « unavailable » pour
de bonnes vidéos — des réglages plus rapides produisent **moins** de données. Cela a été
mesuré, pas supposé.

## Décisions de conception à expliquer

**Un « Video unavailable » nu est réessayé, jamais abandonné.** YouTube renvoie cette
chaîne aussi bien pour une vidéo supprimée que lorsqu'il vous limite, et rien ne les
distingue. La traiter comme définitive supprimerait silencieusement de bonnes vidéos.
Un réessai inutile coûte quelques secondes ; un « gone » erroné est irrécupérable. Toute
erreur non reconnue est réessayée pour la même raison. Voir
[`src/classify.py`](src/classify.py) — le fichier le plus lourd de conséquences, d'où sa
logique pure et ses [tests](tests/test_classify.py).

**Les téléchargements tournent dans un sous-processus.** yt-dlp est la partie qui casse,
et elle casse dans des threads. Une frontière de processus garantit que l'orchestrateur
survit à tout ce que fait le téléchargeur. Elle offre aussi un endroit propre pour
injecter le moteur JS dans le `PATH`.

**La limitation de débit arrête l'exécution.** Pas de réessai, pas de backoff. Continuer
sous limitation marque de bonnes vidéos comme échouées et aggrave le blocage. L'outil
affiche la marche à suivre — changer de réseau, attendre une heure — et sort en ayant
sauvegardé la progression.

**La veille Windows est bloquée explicitement.** Une longue exécution est presque
uniquement de l'attente réseau, que Windows compte comme inactivité et suspend au bout de
20 minutes. `SetThreadExecutionState` bloque cela pendant la durée et relâche à la sortie.
Un capot refermé l'emporte quand même ; l'outil le dit plutôt que de faire semblant.

## Organisation

```
src/classify.py      classification des échecs — gone / blocked / retry
src/downloader.py    encapsulation yt-dlp, concurrence bornée, état SQLite, coupe-circuit
src/audiofolder.py   téléchargements -> train/audio + metadata.csv, et la fusion courante
src/pipeline.py      l'orchestrateur : groupes, reprise, envoi, interface de progression
src/manifest.py      entrées/sorties JSONL / JSON
scripts/             constructeur de manifest
tests/               46 tests, aucun réseau requis
```

## Développement

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
```

## Licence

MIT — voir [LICENSE](LICENSE).

Télécharger de l'audio et le **rediffuser** sont deux questions différentes. Cet outil
fait la première ; le droit de faire la seconde dépend entièrement de ce que vous
collectez. Respectez les conditions d'utilisation de YouTube et les droits attachés aux
contenus récupérés.
