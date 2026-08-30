-- PII-free views the agent's credential is pointed at.
-- Applied by `python scripts/make_views.py`, which substitutes {project}.

CREATE SCHEMA IF NOT EXISTS `{project}.thelook_safe` OPTIONS (location = "US");

-- users: identity and location precision removed. No name, e-mail, street, postal code, coordinates, geometry.
CREATE OR REPLACE VIEW `{project}.thelook_safe.users_safe` AS
SELECT id, age, gender, city, state, country, traffic_source, created_at
FROM `bigquery-public-data.thelook_ecommerce.users`;

-- pass-through views so every table the agent touches lives in the same dataset.
CREATE OR REPLACE VIEW `{project}.thelook_safe.orders` AS
SELECT * FROM `bigquery-public-data.thelook_ecommerce.orders`;

CREATE OR REPLACE VIEW `{project}.thelook_safe.order_items` AS
SELECT * FROM `bigquery-public-data.thelook_ecommerce.order_items`;

CREATE OR REPLACE VIEW `{project}.thelook_safe.products` AS
SELECT * FROM `bigquery-public-data.thelook_ecommerce.products`;
