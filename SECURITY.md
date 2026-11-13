# Sécurité

## Signaler une vulnérabilité

N'ouvrez pas de ticket public. Utilisez le **signalement privé de vulnérabilité** de GitHub : onglet **Security** du
dépôt → **Report a vulnerability**. Indiquez la version (numéro et empreinte de l'image), les étapes pour reproduire et
l'effet constaté. Un accusé de réception est donné dès que possible ; un correctif est publié comme nouvelle version,
avec une note de sécurité.

## Versions suivies

La dernière version publiée reçoit les correctifs de sécurité.

## Ce que l'image garantit

- Exécution sous un utilisateur sans privilège (10001), système de fichiers en lecture seule possible et recommandé,
  aucune capacité Linux requise ;
- aucune migration ni aucune donnée au démarrage ; aucun compte prédéfini ; aucun secret dans les couches ;
- mots de passe hachés (Argon2id), sessions aléatoires dont seule l'empreinte est gardée, protection CSRF, politique de
  sécurité du contenu stricte (aucun script en ligne), en-têtes `X-Content-Type-Options` et `Referrer-Policy` ;
- fichiers importés lus sans exécution (classeurs sans macros, XML sans entités externes) ; images réencodées.

## Maintenance

Questions et anomalies non sensibles : tickets du dépôt GitHub.
