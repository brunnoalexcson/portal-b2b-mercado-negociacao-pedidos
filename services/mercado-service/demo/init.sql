-- Roles e senhas exclusivas do ambiente de demonstração (fora do kit).
CREATE ROLE portal_ddl LOGIN PASSWORD 'mercado_demo_ddl';
CREATE ROLE svc_mercado LOGIN PASSWORD 'mercado_demo_app';
CREATE SCHEMA mercado AUTHORIZATION portal_ddl;
GRANT USAGE ON SCHEMA mercado TO svc_mercado;
-- Tabelas de exemplo como as do database/01-init.sql do kit, para exercitar o DROP da migração 0001 (DG-56).
CREATE TABLE mercado.match (id varchar(64) PRIMARY KEY);
CREATE TABLE mercado.modo_negociacao (id varchar(64) PRIMARY KEY);
CREATE TABLE mercado.processed_events (event_id varchar(64) PRIMARY KEY, processed_at timestamptz);
