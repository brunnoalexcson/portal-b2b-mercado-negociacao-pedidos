-- Roles e senhas exclusivas do ambiente de demonstração.
CREATE ROLE portal_ddl LOGIN PASSWORD 'pedidos_demo_ddl';
CREATE ROLE svc_pedidos LOGIN PASSWORD 'pedidos_demo_app';
CREATE SCHEMA pedidos AUTHORIZATION portal_ddl;
GRANT USAGE ON SCHEMA pedidos TO svc_pedidos;

-- Simula as tabelas de exemplo que o database/01-init.sql do kit cria no schema pedidos:
-- a migração 0001 precisa removê-las (issue #13).
CREATE TABLE pedidos.pedido (id int);
CREATE TABLE pedidos.processed_events (id int);

-- RNF-05: schema vizinho sem permissão para svc_pedidos.
CREATE SCHEMA vizinho;
CREATE TABLE vizinho.registro (id int);
