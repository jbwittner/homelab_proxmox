-- Création d'un locataire. À rejouer à l'identique pour chaque service :
-- c'est ce qui garantit que l'isolation ne dépend pas de l'humeur du jour.
--
--   NAME=forgejo
--   PASS="$(head -c 32 /dev/urandom | base64 | tr -d '\n=+/')"
--   sudo -u postgres psql -v ON_ERROR_STOP=1 \
--        -v name="$NAME" -v password="$PASS" \
--        -f /etc/pgsql-git/tenant.sql
--   echo "$NAME / $PASS"     # → OpenBao
--
-- :"name" cite un identifiant, :'password' cite une chaîne. psql échappe
-- lui-même, donc aucun caractère n'est interdit dans le mot de passe.
--
-- LC_COLLATE 'C' : tri binaire, indépendant de la bibliothèque de locales.
-- Évite toute incohérence d'index si la base change un jour de machine ou de
-- libc, et accélère les comparaisons de texte.

CREATE ROLE :"name" LOGIN PASSWORD :'password';

CREATE DATABASE :"name"
    OWNER       :"name"
    TEMPLATE    template0
    ENCODING    'UTF8'
    LC_COLLATE  'C'
    LC_CTYPE    'C';

-- Sans ceci, tout rôle du cluster peut se connecter à cette base. C'est LA
-- ligne qui fait la différence entre un cluster mutualisé et un cluster
-- partagé par accident.
REVOKE CONNECT ON DATABASE :"name" FROM PUBLIC;
GRANT  CONNECT ON DATABASE :"name" TO :"name";

-- Depuis PG 15, PUBLIC n'a plus le droit de créer dans le schéma public.
-- On le réaffirme, et on s'assure que le propriétaire, lui, l'a bien.
\connect :"name"
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER  SCHEMA public OWNER TO :"name";
GRANT  ALL ON SCHEMA public TO :"name";