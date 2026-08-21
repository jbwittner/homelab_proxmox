-- Base et rôle de Forgejo, dans le cluster CO-LOCALISÉ du CT 400.
--
--   sudo -u postgres psql -v ON_ERROR_STOP=1 -f /etc/forgejo-git/init.sql
--
-- Rejouable : chaque ordre est gardé, rien n'est détruit ni réinitialisé.
-- « fj deploy » le joue lui-même — voir doc/RUNBOOK.md section 3.
--
-- AUCUN MOT DE PASSE, et ce n'est pas un oubli. La connexion se fait par
-- SOCKET UNIX en authentification « peer » : PostgreSQL lit l'identité du
-- processus appelant auprès du noyau. Forgejo tourne sous l'utilisateur
-- système « git »… d'où le MAPPING de pg_ident.conf, qui autorise l'unix-user
-- « git » à endosser le rôle SQL « forgejo » (voir pg_ident.conf de ce même
-- répertoire). Il n'y a donc aucun secret de base de données à faire vivre,
-- à faire tourner, ni à perdre.
--
-- LC_COLLATE 'C' : tri binaire, indépendant de la bibliothèque de locales.
-- Un index reste valide si la base change de machine ou de libc — ce qui est
-- exactement ce qu'un plan de reprise demande.

-- Le rôle, sans mot de passe et sans droit de créer une base : il n'en a
-- besoin ni l'un ni l'autre. Un rôle capable de CREATEDB pourrait contourner
-- l'isolation qu'on pose trois lignes plus bas.
SELECT 'CREATE ROLE forgejo LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE'
    WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'forgejo')
\gexec

-- CREATE DATABASE ne s'exécute pas dans un bloc : \gexec est le seul moyen de
-- le rendre conditionnel sans faire échouer le script au second passage.
SELECT 'CREATE DATABASE forgejo OWNER forgejo TEMPLATE template0 '
       'ENCODING ''UTF8'' LC_COLLATE ''C'' LC_CTYPE ''C'''
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'forgejo')
\gexec

-- LA ligne qui compte. Sans elle, tout rôle du cluster peut se connecter à
-- cette base. Elle est aussi la plus facile à perdre : les ACL ne sont PAS
-- dans un dump, donc une restauration la fait disparaître en silence. D'où
-- « fj verify », qui la relit — et le fait que ce script soit rejouable.
REVOKE CONNECT ON DATABASE forgejo FROM PUBLIC;
GRANT  CONNECT ON DATABASE forgejo TO forgejo;

-- Depuis PG 15, PUBLIC n'a plus le droit de créer dans le schéma public.
-- On le réaffirme, et on s'assure que le propriétaire, lui, l'a bien : c'est
-- Forgejo qui crée ses tables à la première migration de schéma.
\connect forgejo
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER  SCHEMA public OWNER TO forgejo;
GRANT  ALL ON SCHEMA public TO forgejo;
