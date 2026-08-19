-- Gabarit de création d'un locataire. À rejouer à l'identique pour chaque
-- service : c'est ce qui garantit que l'isolation ne dépend pas de l'humeur
-- du jour.
--
--   sed -e 's/@@NAME@@/forgejo/g' -e "s/@@PASSWORD@@/$(pass)/" tenant.sql.tpl \
--     | sudo -u postgres psql -v ON_ERROR_STOP=1
--
-- LC_COLLATE 'C' : tri binaire, indépendant de la bibliothèque de locales.
-- Évite toute incohérence d'index si la base change un jour de machine ou de
-- libc, et accélère les comparaisons de texte.

CREATE ROLE @@NAME@@ LOGIN PASSWORD '@@PASSWORD@@';

CREATE DATABASE @@NAME@@
    OWNER       @@NAME@@
    TEMPLATE    template0
    ENCODING    'UTF8'
    LC_COLLATE  'C'
    LC_CTYPE    'C';

-- Sans ceci, tout rôle du cluster peut se connecter à cette base. C'est LA
-- ligne qui fait la différence entre un cluster mutualisé et un cluster
-- partagé par accident.
REVOKE CONNECT ON DATABASE @@NAME@@ FROM PUBLIC;
GRANT  CONNECT ON DATABASE @@NAME@@ TO @@NAME@@;

-- Depuis PG 15, PUBLIC n'a plus le droit de créer dans le schéma public.
-- On le réaffirme, et on s'assure que le propriétaire, lui, l'a bien.
\connect @@NAME@@
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER  SCHEMA public OWNER TO @@NAME@@;
GRANT  ALL ON SCHEMA public TO @@NAME@@;
