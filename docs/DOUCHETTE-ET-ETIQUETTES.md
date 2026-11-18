# Mettre en service la douchette et les étiquettes

Pour une bénévole ou un bénévole, sur un ordinateur de la bibliothèque, à partir de la version 1.2.0. Comptez une heure
la première fois, à deux. Rien ici ne demande de compétence technique ; chaque étape se vérifie à l'écran avant la
suivante.

**À avoir sous la main** : la douchette (USB ou Bluetooth) et **son manuel** (les codes de réglage y sont imprimés), une
planche d'étiquettes Avery L7160 (21 étiquettes, par exemple pour les BD) ou L7651 (65, format compact), quelques
feuilles de papier ordinaire, une règle graduée, cinq BD prises au hasard.

## 1. Brancher la douchette

Branchez-la en USB : l'ordinateur la reconnaît comme un clavier, **aucun pilote à installer**. En Bluetooth,
appairez-la en mode « clavier » (HID). Ouvrez biblyO et connectez-vous avec **votre** compte.

## 2. Vérifier ce qu'elle envoie

biblyO → bouton **Scanner** (en haut) → **Configurer ma douchette**. Cliquez dans **Scannez ici**, puis scannez les
codes d'essai affichés à l'écran (ou sur la feuille test de l'étape 4 si la douchette lit mal un écran). La **liste de
mise en service** se coche au fil des lectures :

| Point | Ce qu'il faut scanner |
|---|---|
| Code 128 d'essai | le code « BIBLYO-TEST » |
| ISBN-13 | le code « 9782123456803 », ou l'ISBN au dos d'une BD |
| Étiquette d'un livre | l'étiquette biblyO d'un livre (après l'étape 5) |
| Numéro d'inventaire | le numéro d'inventaire d'un livre (scanné, ou tapé puis Entrée) |
| Code inconnu | le code « BIBLYO-INCONNU » : biblyO doit dire « Code inconnu » |
| Trois lectures de suite | trois codes d'affilée, sans attendre |
| Touche finale | vient tout seul si la douchette envoie Entrée ou Tab |
| Clavier | vient tout seul quand le tiret et les chiffres arrivent tels quels |

Si un message apparaît :

| Message | Réglage, avec le manuel de **votre** modèle |
|---|---|
| « Code non reconnu » … « clavier américain (QWERTY) » | scanner le code de réglage « Clavier français » ou « AZERTY » |
| « Suffixe non détecté » | scanner le code de réglage « Suffixe : Entrée » (parfois « Add CR ») |
| « Le dernier chiffre … de l'EAN-13 n'a pas été transmis » | activer « Transmettre le chiffre de contrôle » pour l'EAN-13 |
| « entouré d'autres caractères » | désactiver le préfixe et l'identifiant de symbologie (« AIM ID ») |
| « lentement » | c'était une saisie au clavier : normal sans douchette |

Deux douchettes, même de la même marque, ne se règlent pas forcément pareil. **Notez le modèle exact et les réglages
faits** : ils serviront pour une deuxième douchette.

## 3. Déclarer ce poste comme poste de prêt

Toujours dans **Configurer ma douchette**, cochez **« Ce poste est un poste de prêt de la bibliothèque »**. Une lecture
de douchette ouvre alors la fiche du livre depuis n'importe quelle page. Ne le cochez pas sur un ordinateur personnel.
Les champs de scan (comptoir, retours, scanner) marchent toujours, coché ou non.

## 4. Calibrer l'imprimante (feuille test)

Un bibliothécaire, dans **Codes-barres et étiquettes** → **Réglages des étiquettes** :

1. choisir la planche utilisée (L7160 pour les BD, L7651 pour le format compact) ;
2. **Imprimer la feuille test** sur du **papier ordinaire**, en choisissant dans la fenêtre d'impression **« Taille
   réelle » ou « 100 % »**, jamais « Ajuster à la page » ;
3. mesurer les deux traits de la feuille à la règle : **100 mm chacun** ; sinon, l'impression n'est pas à 100 % ;
4. poser la feuille sur une planche, face à une fenêtre : chaque cadre pointillé doit tomber sur une étiquette. Un
   décalage régulier (tout est 1 mm trop à droite, par exemple) se corrige dans **Calibration**, puis on réimprime ;
5. scanner les trois codes de la feuille imprimée dans **Configurer ma douchette** ;
6. revenir aux réglages : cocher les deux cases et **Certifier cette planche**.

## 5. Première feuille d'étiquettes

1. Un bibliothécaire : **Codes-barres et étiquettes** → **Générer tous les codes manquants** → lire la simulation
   (nombre de livres, premier et dernier code) → **Attribuer**. Pour un autre préfixe que `EX-`, le changer avant
   (`biblyo codes prefixe …`, voir MISE-A-JOUR.md).
2. **Imprimer les étiquettes à faire** → choisir quelques livres d'une même étagère (cote ou emplacement) → la
   planche → l'aperçu (vérifier l'étiquette agrandie) → **Préparer le PDF** → **Télécharger** → imprimer **à 100 %**,
   une seule feuille.
3. Coller les étiquettes, chacune sur son livre (le titre est imprimé dessus).
4. Scanner cinq livres étiquetés : la **bonne fiche** s'ouvre chaque fois. Finir la liste de l'étape 2 (étiquette
   d'un livre, numéro d'inventaire).
5. Au **comptoir** : la carte d'un lecteur d'essai, deux livres, **Confirmer le prêt** ; puis **Retour rapide** : les
   deux livres. Scanner l'ISBN au dos d'une BD : biblyO demande de choisir l'exemplaire, il ne le prend jamais seul.
6. Le lot d'impression : **Imprimées et collées**.

Ensuite, étagère par étagère. Une feuille entamée se réutilise : à l'étape « Choisir la planche », cocher les cases
déjà décollées, ou indiquer la case où commencer.

## Bon à savoir

- **La douchette n'identifie personne.** Un prêt, un retour ou une modification est fait au nom du compte connecté,
  avec ses droits. Se déconnecter en quittant le poste.
- **Qui fait quoi** : générer les codes en masse demande un compte qui gère le catalogue (bibliothécaire) ; imprimer
  demande le droit « Imprimer des étiquettes » (bibliothécaires, et les bénévoles à qui l'on a coché le rôle
  « Impression des étiquettes ») ; un bénévole peut toujours donner un code à un livre depuis sa fiche.
- **Une étiquette abîmée** : depuis la fiche du livre, **Imprimer son étiquette** : le même code, jamais un nouveau.
- **Sans douchette**, tout se fait au clavier : taper le code ou le numéro d'inventaire, puis Entrée.
