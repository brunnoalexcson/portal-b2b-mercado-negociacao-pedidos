-- Roles e senhas exclusivas do ambiente de demonstração.
CREATE ROLE portal_ddl LOGIN PASSWORD 'pedidos_demo_ddl';
CREATE ROLE svc_pedidos LOGIN PASSWORD 'pedidos_demo_app';
CREATE SCHEMA pedidos AUTHORIZATION portal_ddl;
GRANT USAGE ON SCHEMA pedidos TO svc_pedidos;
