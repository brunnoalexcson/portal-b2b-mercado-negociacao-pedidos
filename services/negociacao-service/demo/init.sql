-- Roles e senhas exclusivas do ambiente de demonstração.
CREATE ROLE portal_ddl LOGIN PASSWORD 'negociacao_demo_ddl';
CREATE ROLE svc_negociacao LOGIN PASSWORD 'negociacao_demo_app';
CREATE SCHEMA negociacao AUTHORIZATION portal_ddl;
GRANT USAGE ON SCHEMA negociacao TO svc_negociacao;

-- Imitação das tabelas de exemplo que o kit cria no schema, para a migração 0001 provar que as apaga.
-- No kit (database/01-init.sql) elas também são criadas como portal_ddl, dona do schema.
SET ROLE portal_ddl;
CREATE TABLE negociacao.negociacao (id serial PRIMARY KEY, descricao text);
CREATE TABLE negociacao.lance (id serial PRIMARY KEY, negociacao_id integer REFERENCES negociacao.negociacao(id),
                               valor numeric);
CREATE TABLE negociacao.idempotency_keys (chave text PRIMARY KEY);
CREATE TABLE negociacao.processed_events (event_id text PRIMARY KEY);
RESET ROLE;
