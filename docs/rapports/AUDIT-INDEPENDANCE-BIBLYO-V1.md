# Audit d'indépendance et de provenance de biblyO 1.0.0

Audit du 13 novembre 2026, branche `audit/independance-v1`. Il précède toute autorisation de publication publique.
Ce n'est ni un avis juridique ni une certification : il établit des faits vérifiables, signale ce qui reste incertain
et dit ce qui doit être validé par une personne compétente en droit.

---

## 1. Périmètre et versions

### biblyO, objet de l'audit

| Élément | Version |
|---|---|
| Instantané de distribution (dépôt privé `ADSOLUTIONPRO/BiblyO`) | `main@c1157a6`, version 1.0.0 |
| Code d'origine | `main@e3419c9` du dépôt de développement privé (194 commits, du 6 au 12 novembre 2026) |
| Fichiers comparés | `src/`, `tests/`, `scripts/`, `deploy/`, `Dockerfile` : 301 Python, 112 gabarits HTML, 9 JavaScript (hors bibliothèques tierces), 5 CSS, 2 SVG ; 73 312 lignes Python |

### Application antérieure : Shelf

Logiciel libre (AGPL-3.0, dépôt public `dgahagan/shelf`), de même pile technique (FastAPI, SQLite, HTMX). Une
installation, enrichie de modules locaux en français, a précédé biblyO dans la structure pour laquelle il a été conçu.

| Corpus comparé | Version | Contenu analysé |
|---|---|---|
| Dépôt public, tête | `39edc43` (12 novembre 2026, après v0.79.0) | 467 Python, 85 HTML, 27 JS, 2 CSS, 1 SVG, 4 catalogues `.po` |
| Version d'origine de l'installation antérieure | étiquette `v0.73.0` (image utilisée à l'époque : `0.73.0`) | 432 Python, 84 HTML, 27 JS, 2 CSS, 1 SVG |
| Modules locaux de l'installation antérieure (archives privées) | état archivé | 34 Python, 14 HTML, 9 JS, 7 CSS : traductions, correctifs, image de marque, prêts, BnF, inventaire |

Les archives et les copies de Shelf ont été lues dans un dossier de travail temporaire, sans modification, et ne sont
pas versionnées. Les fichiers de données qu'elles contiennent (classeurs, CSV, clés) n'ont pas été analysés et ne
sont repris nulle part.

Le dépôt de développement privé a aussi été parcouru dans tout son historique (194 commits) : aucun fichier de Shelf
ni des modules locaux n'y a jamais été ajouté. Les identifiants internes de Shelf n'y apparaissent que dans le dossier
de conception initial, pour les proscrire.

## 2. Méthode et limites

### Méthode

Chaque fichier de biblyO a été comparé aux trois corpus par des mesures indépendantes, puis chaque rapprochement a été
lu à la source, côte à côte :

| Mesure | Ce qu'elle repère |
|---|---|
| Empreintes de fichiers (sha256) | Fichier copié tel quel, image ou police comprise |
| Jetons normalisés (noms, chaînes, nombres remplacés par leur catégorie), *winnowing* k = 25 | Copie dont les noms ont été changés |
| Suites de jetons alignées, part de jetons bruts identiques dans chaque suite | Distinguer une même forme d'un même texte |
| Arbres syntaxiques Python normalisés (blocs d'au moins 40 nœuds) | Même structure de fonction ou de boucle, noms changés |
| Lignes identiques non triviales (35 caractères et plus) | Copie de lignes |
| Chaînes littérales et textes d'interface, en particulier français (10 caractères et plus) | Reprise de traductions, messages, libellés |
| Commentaires et docstrings | Reprise de commentaires |
| Requêtes SQL | Requêtes caractéristiques |
| Variables et sélecteurs CSS | Reprise d'une feuille de style |
| Tracés SVG | Reprise d'icônes |
| Noms de fonctions et de classes, puis corps des homonymes | Reprise d'une organisation du code |

### Limites

- Une mesure automatique ne détecte que ce qu'elle cherche. Elle ne prouve pas une indépendance : elle rend une reprise
  substantielle **improbable sans être vue**, et chaque rapprochement a été relu à la main.
- La reprise d'une idée, d'une fonction ou d'un parcours n'est pas mesurable ainsi. biblyO couvre des besoins proches
  de ceux de Shelf (catalogue, prêts, emplacements) : ces besoins communs ne sont pas, en eux-mêmes, une reprise.
- Les corpus sont la tête publique, la version d'origine et les modules locaux archivés. Des versions intermédiaires de
  Shelf n'ont pas été comparées une à une. La tête et la v0.73.0 couvrent l'essentiel du code.
- Le code de biblyO a été écrit avec une assistance par IA (section 9). Un modèle peut restituer des tournures vues
  pendant son entraînement. La comparaison couvre Shelf, pas l'ensemble du code public existant.

## 3. Inventaire des rapprochements

| Classement | Nombre | Détail |
|---|---|---|
| **Reprise établie** | **0** | Aucune |
| **Forte suspicion** | **0 dans le code**, 7 dans les anciennes icônes | Les icônes de la maquette proches de Feather Icons sont **retirées** (section 7) |
| **Générique ou non significatif** | 12 familles | Section 4 : idiomes de Python, de FastAPI, de Playwright, du DOM, formats publics |
| **Aucune correspondance notable** | Tout le reste | En particulier gabarits, JavaScript, CSS, migrations, modèle de données, import, prêts, couvertures, enrichissement, rangement |

Chiffres bruts (avant revue) :

| Mesure | Tête 39edc43 | v0.73.0 | Modules locaux |
|---|---|---|---|
| Fichiers identiques | 1 (fichier vide) | 1 (fichier vide) | 0 |
| Blocs Python de même arbre syntaxique (≥ 40 nœuds) | 0 | 0 | 0 |
| Plus forte part d'empreintes normalisées communes, par fichier | 9,5 % (test de 169 empreintes) | 9,5 % | 10 % |
| Plus longue suite de jetons **bruts** commune | 20 jetons (≈ 2 lignes) | 20 | 20 |
| Plus longue suite **logique** commune (k-grammes porteurs de logique) | 45 jetons | 45 | < 45 |
| Commentaires communs (20 caractères et plus) | 3 (un shebang, deux adresses d'API publiques) | 2 | 2 |
| Textes d'interface français communs (10 caractères et plus) | 0 : Shelf n'a pas de traduction française (italien et chinois seulement) | 0 | 36 sur 10 085, vocabulaire courant |
| Noms de fonctions et classes communs (12 caractères et plus) | 13 sur 943, corps différents | — | — |
| Tracés SVG communs | 1 : le menu « trois traits » de l'ancien sprite | 1 | 0 |

## 4. Preuves et localisations

Chaque ligne a été lue côte à côte. Fichiers biblyO à l'état `main@c1157a6`.

| biblyO | Shelf | Nature | Constat | Risque | Classement, action |
|---|---|---|---|---|---|
| 47 gabarits (`templates/**`) : `<input type="hidden" name="_csrf" value="{{ csrf_token }}">` | `app/templates/fragments/periodical_scan_result.html` | Ligne identique | Champ caché CSRF : la forme standard, imposée par le jeton | Nul | Générique, aucune |
| `core/errors.py`, routes de 9 modules : `raise HTTPException(status_code=404)`, en-tête `Cache-Control: no-store` | `app/routers/settings.py`, `app/routers/api_v1.py` | Lignes identiques | API de FastAPI, écriture unique | Nul | Générique, aucune |
| `migrations/versions/0003_recherche.py:44-46` (`_normalize`) | `app/services/authors.py:49-51` (`normalize`) | Même suite NFKD puis suppression des diacritiques | Recette canonique de Python pour ôter les accents. Différences : table de ligatures propre, `casefold` placé autrement, sortie chaîne contre liste de mots, motif distinct | Très faible | Générique, aucune |
| `modules/enrichment/covers.py:103-108` (cache des vignettes) | `app/main.py:362-367` (limiteur de débit) | Même forme `move_to_end` / `popitem(last=False)` | Cache LRU sur `OrderedDict`, idiome documenté par Python ; fonctions différentes | Nul | Générique, aucune |
| `modules/imports/simulate.py:231-290`, `catalogue/repository.py`, `patrons/repository.py:706-717` | `app/services/audits.py:310-318`, `app/services/duplicates.py:59-72` | Suites normalisées communes | Listes de champs `@dataclass` ; noms, types et sens tous différents | Nul | Générique, aucune |
| `modules/imports/profile.py:34-202`, `catalogue/search.py:62-77` | `app/config.py:14-181`, modules locaux `bnf` | Suites normalisées communes | Tables de libellés `{"clé": _("Libellé")}` ; contenus entièrement différents | Nul | Générique, aucune |
| `modules/circulation/counter.py:109-115` | `app/features.py:55` | Suite normalisée | Champs `tuple[...] = ()` d'une dataclass | Nul | Générique, aucune |
| `core/security/headers.py` (`SecurityHeadersMiddleware`), `core/security/passwords.py` (`hash_password`, `verify_password`), `catalogue/routes.py` (`cover_from_url`), `holdings/service.py` (`create_location`…) | `app/main.py`, `app/auth.py`, `app/routers/items_covers.py`, `app/routers/locations.py` | Mêmes noms | Corps sans rapport : middleware ASGI pur contre `BaseHTTPMiddleware`, Argon2 contre bcrypt, ORM contre SQL écrit à la main | Nul | Générique, aucune |
| `tests/e2e/conftest.py:128-133` (`login`) et autres parcours | `tests/e2e/test_lending.py:124-128` | Suites courtes | Connexion Playwright (`goto`, `fill`, `click`, `wait_for_url`) ; sélecteurs et adresses différents | Nul | Générique, aucune |
| `scripts/check_*.py`, `tests/unit/test_checks.py` | `scripts/check_alpine_csp.py`, `check_pr_generated.py` | 20 jetons bruts | Préambule `import re / sys / Path` puis `ROOT = Path(__file__).resolve().parent.parent` ; chargement par `importlib.util` | Nul | Générique, aucune |
| `static/js/app.js`, `components/shelves.js` | modules locaux `v09/v09.js`, `v12/virtual-shelves-fr.js` | Suites normalisées courtes | `querySelector`, `closest`, `createElement` : API du DOM ; aucune ligne commune | Nul | Générique, aucune |
| `tests/fixtures/sources/**` (réponses BnF et Sudoc) | `tests/fixtures/bnf_sru_*.xml` | Lignes XML communes | Format MARCXchange de la BnF (balises, zones, mentions de collection). **Aucune notice commune** : 63 identifiants ARK chez biblyO, 3 chez Shelf, intersection vide ; ISBN différents | Nul | Générique (donnée publique, Licence Ouverte), aucune |
| `static/src/*.css` | `static/css/*.css` | 6 variables, 19 sélecteurs communs | `--font-sans`, `--font-mono`, `--radius-sm/md/lg` (vocabulaire de Tailwind), `body`, `h1`, `:root`, `[x-cloak]` | Nul | Générique, aucune |
| Ancien `static/icons/sprite.svg`, symbole `menu` | `app/templates/base.html`, `browse.html` | Tracé identique `M4 6h16M4 12h16M4 18h16` | Trois traits horizontaux, tracé le plus simple possible | Nul | Générique ; **remplacé** de toute façon (section 7) |
| Textes français : « mot de passe actuel », « prêts en cours », « tester la connexion »… (36) | Modules locaux de traduction | Chaînes communes | Vocabulaire d'interface courant ; aucune phrase longue ou tournure propre ; Shelf n'a aucune traduction française | Nul | Générique, aucune |

Conclusion : **aucune reprise de code, de gabarit, de style, de traduction ou de texte de Shelf ou de ses modules locaux
n'a été trouvée** dans biblyO 1.0.0. Tous les rapprochements relèvent des idiomes du langage, des cadres employés ou de
formats publics.

## 5. Corrections

Aucun code n'a dû être réécrit : aucune reprise problématique n'a été trouvée dans le code. Les corrections portent sur
les ressources et les contrôles.

| Correction | Fichiers | Raison |
|---|---|---|
| Les 53 icônes de la maquette, d'origine inconnue, remplacées par 53 icônes **Lucide 1.70.0 non modifiées** | `static/icons/sprite.svg`, `static/icons/sources.json`, `scripts/build_icons.py` | Sept d'entre elles étaient presque identiques à Feather Icons, sans attribution établie ; les autres n'avaient pas d'auteur connu (section 7) |
| Sprite reconstruit et comparé au caractère près en CI, depuis le paquet npm vérifié par son empreinte sha512 | `.github/workflows/ci.yml`, `tests/unit/test_icons.py` | Empêcher toute retouche manuelle ou tout tracé d'origine inconnue |
| Licence Feather jointe « par précaution » retirée ; texte de Lucide conservé à l'identique (il contient la licence MIT de Feather pour les icônes dérivées) | `static/icons/licenses/` | Les licences jointes correspondent désormais aux éléments réellement repris |
| Outil de mesure de ressemblance des icônes retiré | `scripts/measure_icons.py` | Sans objet : la provenance est établie |
| Nouveau contrôle durable de reprise de code | `scripts/check_provenance.py`, `scripts/provenance/` | Ci-dessous |
| Composants de l'image (paquets Debian avec leur paquet source, paquets Python avec leur licence) joints à chaque version | `.github/workflows/publication.yml`, `THIRD_PARTY_NOTICES.md` | Obligations des licences GPL et LGPL de paquets Debian redistribués dans l'image (section 8) |
| Versions, titulaires et absence de nom réservé des polices consignés | `THIRD_PARTY_NOTICES.md` | Section 6 |
| Renvoi d'un document de conception absent de la distribution corrigé | `scripts/check_independence.py` | Exactitude |

### Contrôle durable de reprise de code

`scripts/check_provenance.py`, lancé par `make lint` donc par la CI et par le flux de publication :

- **Référence sans code tiers** : `scripts/provenance/reference.bin` (1,5 Mo) ne contient que des empreintes de 40 bits,
  à sens unique, de suites de jetons des trois corpus. Aucun texte, nom ou fichier de Shelf n'y figure ni ne peut en
  être reconstruit. Les copies de Shelf servant à la reconstruire restent hors dépôt
  (`check_provenance.py --reference nom=dossier…`).
- **Deux mesures** : suites de 20 jetons bruts (copie), et suites de 32 jetons normalisés porteuses d'au moins quatre
  mots-clés ou opérateurs distincts (copie renommée, sans signaler les tables de libellés de même forme).
- **Seuils** : échec dès 30 jetons bruts ou 60 jetons logiques communs, sauf suite examinée et justifiée dans
  `scripts/provenance/examines.json` (vide aujourd'hui). État actuel : 20 et 45 au plus.
- **Essais** : `tests/unit/test_check_provenance.py` vérifie qu'une copie est refusée, qu'une copie dont tous les noms
  sont changés l'est aussi, qu'un code sans rapport passe, qu'une suite examinée est acceptée et qu'une référence
  altérée est refusée.
- `scripts/check_independence.py` (identifiants et noms de fichiers hérités) est conservé.

Un résultat favorable signifie « aucune reprise substantielle détectée par cette méthode », pas « indépendance certifiée ».

## 6. Polices

Les six fichiers livrés sont **identiques, octet pour octet**, à ceux des paquets npm officiels Fontsource 5.3.0, et les
textes de licence joints sont ceux de ces paquets, à l'identique.

| Police | Fichiers | Titulaire (inscrit dans la police) | Auteur, source officielle | Version | Licence |
|---|---|---|---|---|---|
| Bricolage Grotesque | `bricolage-grotesque-latin(-ext)-opsz-normal.woff2` | © 2022 The Bricolage Grotesque Project Authors | Atelier Triay (Mathieu Triay), `github.com/ateliertriay/bricolage` | 1.001 | SIL OFL 1.1 |
| Manrope | `manrope-latin(-ext)-wght-normal.woff2` | © 2019 The Manrope Project Authors | Mikhail Sharanda, `github.com/sharanda/manrope` | 4.504 | SIL OFL 1.1 |
| JetBrains Mono | `jetbrains-mono-latin(-ext)-wght-normal.woff2` | © 2020 The JetBrains Mono Project Authors | JetBrains, `github.com/JetBrains/JetBrainsMono` | 2.211 | SIL OFL 1.1 |

| Question | Réponse |
|---|---|
| Usage commercial | Permis. L'OFL interdit seulement de vendre les polices **seules** ; biblyO les fournit avec le logiciel |
| Intégration web, redistribution | Permises ; `fsType` = 0 (aucune restriction d'intégration) dans les six fichiers |
| Mentions | Copyright et texte de l'OFL joints (`static/fonts/licenses/`) et cités dans `THIRD_PARTY_NOTICES.md` |
| Modifications | Aucune par biblyO. Le découpage en sous-ensembles latin et latin étendu est celui de Fontsource. Une police découpée reste sous OFL |
| Noms de fonte réservés | Aucun déclaré dans les trois licences : l'emploi des noms d'origine est permis |
| Intégrité | Empreintes sha256 figées et contrôlées (`scripts/check_vendor.py`) |
| Propriété | Les polices restent la propriété de leurs auteurs : elles ne sont pas présentées comme appartenant au projet |

**Conclusion : conformes, conservées.** Aucune autre police n'est livrée. La feuille de style ne charge aucune police
externe.

## 7. Icônes et médias

### Icônes : remplacées

L'ancien sprite reprenait 53 tracés de la maquette d'interface, sans source documentée. La mesure du 7 novembre 2026
les rapprochait de Feather Icons 4.29.2 et de Lucide 1.70.0. L'analyse icône par icône (`docs/provenance/icones.md`,
historique) les classait ainsi :

| Groupe | Icônes | Lecture |
|---|---|---|
| Très proches de Feather (recouvrement 0,94 à 1,00 ; `shield` identique au caractère près) | `shield`, `edit`, `bell`, `alert`, `heart`, `moon`, `gear` | Reprise de Feather **plausible**, sans attribution |
| Proches de Lucide (0,86 à 0,87) | `eye`, `locate` | Ressemblance modérée |
| Formes élémentaires (0,89 à 1,00) | `plus`, `x`, `chev`, `chevd`, `out`, `menu`, `ret` | Peu significatif |
| Autres (sous 0,85) | 37 icônes | Aucune ressemblance marquée, mais aucun auteur connu |

Garder avec attribution supposait de connaître l'auteur, ce qui n'est vrai d'aucune d'elles. Les redessiner aurait laissé
la même incertitude (le dessinateur aurait été un outil d'IA), et retoucher un tracé pour masquer une ressemblance est
exclu. **Les 53 ont été remplacées par 53 icônes Lucide 1.70.0 choisies pour leur sens**, reprises sans modification
(ISC ; MIT pour celles dérivées de Feather). La correspondance est publiée dans `docs/provenance/icones.md`.

### Autres médias

| Élément | Provenance | Statut |
|---|---|---|
| Logo (`static/icons/logo.svg`, macro `logo`) : carré arrondi, barre, anneau | Maquette produite pour le projet avec un outil d'IA (section 9) | Formes géométriques élémentaires. Aucune ressemblance avec le logo de Shelf (livre ouvert blanc sur fond violet) ni avec un jeu tiers connu. Sa protection par le droit d'auteur est incertaine (section 9) ; **une vérification de disponibilité du nom et du signe « biblyO » à titre de marque n'a pas été faite** |
| Favicon | Aucun fichier distinct : le navigateur reçoit le logo | — |
| Anneaux, barres, mini-plans | Formes calculées par les gabarits | Code de biblyO |
| `tests/fixtures/couverture-essai.jpg` | Aplat de couleur 120 × 180 produit pour les essais | Aucun contenu tiers |
| Images de démonstration, couvertures, illustrations | Aucune n'est versionnée | — |
| Maquette (`biblyo-maquette.html`) | Production du projet ; contient des titres réels d'une bibliothèque | **Non publiée** ; ses styles ont été réécrits dans `static/src/` |

## 8. Dépendances

### Livrées dans l'application

| Composant | Version | Licence | Usage commercial, redistribution, modification | Obligations |
|---|---|---|---|---|
| htmx | 2.0.11 | 0BSD | Permis | Aucune (mention conservée) |
| Alpine.js, build CSP | 3.17.5 | MIT | Permis | Copyright et licence joints |
| Polices (section 6) | Fontsource 5.3.0 | OFL 1.1 | Permis, pas de vente seule | Copyright et licence joints |
| Lucide | 1.70.0 | ISC ; MIT pour la part Feather | Permis | Copyright et licence joints |
| Feuille produite par Tailwind CSS | v4.3.3 | MIT | Permis | Mention dans `THIRD_PARTY_NOTICES.md` |

### Python, à l'exécution (47 paquets, verrouillés par `uv.lock`)

Tous sous licence permissive : MIT (dont alembic, fastapi, sqlalchemy, pydantic, openpyxl, typer, argon2-cffi,
uvloop), BSD (starlette, uvicorn, jinja2, httpx, click, babel), ISC (shellingham), Apache-2.0 (python-multipart,
tzdata, opentelemetry-api), PSF (defusedxml, typing-extensions), MIT-CMU (Pillow, avec FreeType, libjpeg-turbo, libpng,
zlib et autres sous licences permissives, `docs/provenance/pillow.md`), MIT-0 (cffi). Un seul copyleft faible :
**certifi, MPL-2.0**, utilisé sans modification (l'obligation de la MPL ne porte que sur ses propres fichiers modifiés).
`colorama` n'est installé que sous Windows. Aucun paquet sous GPL, LGPL ou AGPL.

### Image Docker

| Composant | Licence | Obligation |
|---|---|---|
| Base `python:3.12-slim` (Debian 13), figée par empreinte | Licence PSF pour Python ; paquets Debian sous leurs licences, dont GPL et LGPL (bash, coreutils, glibc…) | Redistribués sans modification : fournir l'accès aux sources correspondantes. **Corrigé** : chaque version joint la liste exacte des paquets source et versions, avec l'adresse des sources Debian (section 5) |
| `uv` 0.13.7 | MIT ou Apache-2.0 | Sert à la construction, retiré de l'image finale |

### Outils non livrés

Tailwind CSS (MIT), Playwright (Apache-2.0), axe-core (MPL-2.0, par axe-playwright-python, MIT), pytest et outils de
qualité : usage de développement seulement.

### Données de test

Réponses de la BnF et du Sudoc (Abes) sous **Licence Ouverte** de l'État : réutilisation commerciale permise, avec
mention de la source et de la date, présente dans `tests/fixtures/sources/README.md`. Aucune notice n'est commune avec
les fixtures de Shelf.

**Compatibilité avec l'EUPL-1.2** : aucune de ces licences n'impose sa propre licence au code de biblyO. Les licences
permissives et la MPL-2.0 (au niveau du fichier) se combinent sans conflit avec l'EUPL-1.2.

## 9. Provenance assistée par IA

| Contribution | Qui | Trace |
|---|---|---|
| Besoin, conception, priorités, arbitrages (lots L0 à L6, décisions de données, de sécurité, de publication), validation de chaque lot et de chaque fusion | Le responsable du projet (compte ADSOLUTIONPRO) | Décisions consignées dans les rapports de lot et les documents de conception (dépôt privé), commits publiés sous ce compte |
| Spécifications détaillées, architecture, plans de lot | Rédigés avec Claude Code à partir des demandes du responsable, puis relus et arbitrés par lui | Dossier de conception (dépôt privé) |
| Code, tests, scripts, documentation | Écrits par Claude Code (modèles Claude Opus 5.5, et Claude Fable 5.1 pour six commits), sur instruction | 194 commits sur 194 portent la mention « Co-Authored-By: Claude » |
| Maquette d'interface, jetons de couleur, logo | Produits avec Claude (Artifacts) sur le compte du projet, choisis par le responsable | Registre de provenance (dépôt privé) |
| Revues | Revue par le responsable (validations visuelles, essais sur ses données, arbitrages) ; revues outillées (tests, contrôles, audits) | Rapports de lot |
| Autres contributeurs humains | Aucun connu | — |
| Code de tiers | Uniquement les composants de la section 8, sous leurs licences | `THIRD_PARTY_NOTICES.md` |

Points d'attention, qui demandent une **validation juridique** :

1. **Protection des productions de l'IA.** En droit français et européen, le droit d'auteur protège une création
   originale qui porte l'empreinte de la personnalité d'un auteur humain. Ce qu'un outil d'IA produit sans apport
   créatif humain suffisant peut **ne pas être protégé**. La part protégeable de biblyO tient sans doute aux choix
   humains (conception, sélection, arrangement, arbitrages), mais son étendue n'est pas établie.
2. **Conditions d'utilisation de l'outil.** Les conditions commerciales d'Anthropic applicables au compte utilisé
   déterminent à qui reviennent les productions. Elles doivent être relues pour le compte et l'offre réellement
   employés. Ce rapport ne les présume pas.
3. **Conséquence pratique** : une licence libre peut être concédée sur l'ensemble, mais on ne peut pas affirmer que
   chaque ligne est protégée par un droit d'auteur appartenant au titulaire. Cela pèse surtout sur la capacité à
   **interdire** des usages ou à **changer de licence** pour des versions futures, moins sur la diffusion.

## 10. Registre des droits

| Élément | Statut | Détail |
|---|---|---|
| Absence de code, gabarit, style, traduction ou texte de Shelf | **Connu** | Sections 3 et 4 ; contrôlé à chaque envoi |
| Licences des composants tiers et de l'image | **Connu** | Section 8 ; compatibles avec un usage commercial et avec l'EUPL-1.2 |
| Polices | **Connu** | OFL 1.1, fichiers officiels non modifiés |
| Icônes | **Connu** | Lucide 1.70.0, ISC et MIT, non modifiées |
| Données de test | **Connu** | Licence Ouverte, sources mentionnées |
| **Titulaire des droits sur biblyO** | **Décidé (L1), nom à inscrire** | Décision du 13 novembre 2026 : le responsable du projet, à titre personnel, est titulaire des droits qu'il détient sur les éléments originaux et protégeables. ADSOLUTIONPRO est seulement le compte de publication. Mention `Copyright © 2026 [NOM CIVIL DU TITULAIRE]` puis `Licensed under the EUPL-1.2` dans `NOTICE`, `README.md` et la page « À propos » ; **le nom civil à afficher reste à confirmer** : `scripts/check_titulaire.py --publication` refuse toute publication tant qu'il n'est pas inscrit |
| **Statut des productions de l'IA** | **Décidé (L2) : assumé et documenté** | Décision du 13 novembre 2026 : l'assistance de Claude Code est assumée ; les contributions humaines (conception, décisions, arbitrages, validations) sont conservées dans l'historique et les rapports ; il n'est pas affirmé que chaque ligne produite par l'IA fait l'objet d'un droit d'auteur exclusif (`NOTICE`, `README.md`). L'étendue exacte de la protection reste une question de droit, sans effet sur la diffusion sous EUPL-1.2 |
| Contributeurs et cessions | **Connu à ce jour** | Aucun autre contributeur. Avant d'accepter une contribution externe : décider d'un accord de contribution (cession ou licence, ou certificat d'origine) |
| Maquette, logo, jetons | **Incertain** | Productions d'un outil d'IA sur le compte du projet : même question que L2 |
| Nom et logo « biblyO » comme marque | **Risque signalé** | Recherche sur le web du 13 novembre 2026 (non exhaustive, sans valeur de garantie) : aucune marque « Biblyo » enregistrée trouvée ; mais une application Android **« Biblyo »** (Blue Carpet Group B.V., Pays-Bas), liée à une bibliothèque, existe sur les magasins d'applications, et **« Biblys »**, logiciel libre français pour libraires, porte un nom proche. Les bases officielles (INPI, EUIPO/TMview, OMPI) n'ont pas été interrogées. Une licence libre ne protège pas un nom |
| Licence EUPL-1.2 | **Confirmée** | Décision du 13 novembre 2026 ; texte officiel intact dans `LICENSE` ; page « À propos » mise à jour |
| L'EUPL-1.2 dans la liste des licences admises pour les administrations (article D. 323-2-1 du CRPA) | **À vérifier (L3)** | Utile pour une collectivité |
| Installation antérieure de Shelf modifiée (hors biblyO) | **À traiter à part** | Ses modules locaux sont des modifications d'un logiciel AGPL-3.0 servi en réseau : tant qu'elle sert des utilisateurs, l'article 13 de l'AGPL demande de leur offrir le code source modifié. Sans effet sur biblyO, qui n'en reprend rien |

### Ce qu'implique l'EUPL-1.2

- **Ce qu'elle fait** : le titulaire concède à tous un droit d'utiliser, copier, modifier et redistribuer biblyO, y
  compris à des fins commerciales. Toute version modifiée **distribuée ou communiquée** (y compris servie en réseau à
  des utilisateurs) doit rester sous EUPL-1.2 ou sous une licence compatible de son annexe, avec son code source.
- **Ce qu'elle ne fait pas** : elle ne transfère pas la propriété. Le titulaire reste titulaire.
- **Vente** : rien n'empêche de vendre biblyO, une installation, un hébergement, un support ou une personnalisation.
  Mais un client peut redistribuer le code reçu sous la même licence.
- **Licence future** : le titulaire peut publier les versions futures sous une autre licence, ou en double licence
  (par exemple une licence commerciale à côté), à condition d'être seul titulaire, ou d'avoir obtenu les droits des
  contributeurs. Les versions déjà publiées restent sous EUPL-1.2 pour ceux qui les ont reçues. Le point L2 limite
  cette certitude pour la part produite par l'IA.
- **Compatibilité** : l'annexe de l'EUPL-1.2 permet notamment de combiner avec la GPL, l'AGPL, la MPL, la LGPL et
  quelques autres. Aucun composant actuel n'impose sa licence.

## 11. Non-régression

Le code applicatif n'a pas changé. Seuls changent le sprite d'icônes (même nom pour chaque symbole), la documentation,
les contrôles et le flux de publication. Aucun schéma, aucune migration, aucune donnée n'est touché : **pas d'analyse de
migration nécessaire**. Les révisions 0001 à 0012 sont inchangées, de même que l'import, l'export, les rôles, les
couvertures, les prêts et le rangement.

| Vérification | Résultat |
|---|---|
| `make check` (lint, types, contrôles d'indépendance, de reprise et de titulaire, gabarits, design, composants tiers, i18n, Docker, tests) | 3 044 tests réussis à l'audit ; 3 049 après les mentions de propriété (page « À propos », titulaire) |
| Contrôle de reprise | Plus longues suites : 20 jetons bruts et 45 logiques, pour des seuils de 30 et 60 |
| Icônes | 53 symboles, identiques à Lucide 1.70.0 (`build_icons.py --check`) ; chaque icône appelée existe |
| Parcours navigateur (`make e2e`, Chromium, contrôles d'accessibilité axe compris) | 179 parcours réussis (6 min) |
| Répétition du flux de publication sur GitHub (linux/amd64 natif : contrôles, navigateur, image reproductible, Trivy, recette complète du paquet Synology avec sauvegarde, restauration et restauration isolée, liste des composants de l'image), sans envoi ni version | Run 43481548875 sur `11285f8`, **réussi** : contrôles (3 044 tests, reprise 20 / 45 jetons, icônes identiques à Lucide, pip-audit sans vulnérabilité connue), 179 parcours navigateur, image `sha256:96bb5a6b…`, Trivy sans critique corrigeable (Debian 13.7), recette 33/33 puis 5/5 après redémarrage, sauvegarde, restauration en place et restauration isolée conformes, 122 Mio de mémoire ; 90 lignes de composants Debian et 47 paquets Python listés ; aucun envoi |

L'aller-retour exact des 929 exemplaires et 12 090 cellules a été vérifié au lot L6 sur le code inchangé depuis. Ce
code, l'import et l'export, n'est pas modifié par cet audit, et son contrôle automatisé reste dans la suite de tests.
Les données réelles ne sont pas dans ce dépôt.

## 12. Risques résiduels

| Risque | Niveau | Traitement |
|---|---|---|
| Une reprise de Shelf non détectée par les mesures | Faible | Six mesures indépendantes, revue manuelle, historique complet vérifié ; contrôle permanent en CI |
| Restitution par l'IA de code public autre que Shelf | Faible, non mesuré | Le code est très spécifique (français, règles métier propres) ; pas de mesure exhaustive possible. À surveiller si un tiers signale une ressemblance |
| Nom civil du titulaire pas encore inscrit | **Bloquant pour la publication**, verrouillé | Remplacer `[NOM CIVIL DU TITULAIRE]` dans `NOTICE`, `README.md`, `src/biblyo/__init__.py` ; la publication d'une étiquette échoue sinon |
| Protection incertaine des productions de l'IA (L2) | Moyen, assumé | Documenté ; avis juridique recommandé avant une stratégie commerciale fondée sur l'exclusivité |
| Conflit de nom avec « Biblyo » (application néerlandaise de bibliothèque) ou « Biblys » | Moyen, non mesuré | Recherche d'antériorité dans les bases officielles (INPI, EUIPO, OMPI), au mieux par un conseil en propriété industrielle, avant une diffusion large ou un dépôt de marque |
| Obligations des paquets GPL de l'image | Faible | Liste et sources jointes à chaque version (corrigé) |
| Installation antérieure de Shelf modifiée encore en service | Hors biblyO | Offrir les modules locaux à ses utilisateurs (AGPL, article 13) ou retirer l'instance |
| Faux sentiment de sécurité d'un contrôle automatique | Faible | Le contrôle et ce rapport le disent : un score n'est pas une certification |

## 13. Décision recommandée

**Côté code et ressources : publiable.** Aucune reprise de Shelf n'a été trouvée. Les seules ressources d'origine
inconnue, les icônes, sont remplacées par des icônes de licence connue. Polices, bibliothèques, dépendances et données
de test sont identifiées, compatibles avec un usage commercial et avec l'EUPL-1.2, et leurs mentions sont présentes.

**Décisions du 13 novembre 2026** : titulaire personnel (L1), EUPL-1.2 confirmée, assistance par IA assumée et
documentée (L2). Mentions en place dans `NOTICE`, `README.md`, `docs/PROVENANCE.md` et la page « À propos ».

**Dernier point bloquant : le nom civil du titulaire**, à confirmer puis inscrire à la place de
`[NOM CIVIL DU TITULAIRE]` (trois fichiers). La publication d'une étiquette est refusée automatiquement d'ici là.

Recommandé sans être bloquant : recherche d'antériorité du nom « biblyO » dans les bases officielles (risque signalé
section 10), et traitement de l'installation antérieure de Shelf modifiée.

Rien n'a été publié : pas d'étiquette `v1.0.0`, pas d'image GHCR, dépôt toujours privé, historique publié inchangé.
