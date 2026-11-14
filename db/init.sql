-- 结算样表：商户表 + 结算订单表
-- 数据刻意做成倾斜分布：类别 A 的 100 个商户沉淀了 70 万单（占 70%），
-- 其余 4900 个商户共分 60 万单。配合页面示例 SQL 里的表达式过滤
-- （优化器只能用默认选择率，把 100 个商户估成 25 个），默认计划会误选
-- 嵌套循环——这正是迁库后夜间结算变慢的常见诱因，方便演示对照效果。

CREATE TABLE merchants (
    merchant_id   integer PRIMARY KEY,
    merchant_name text        NOT NULL,
    category      text        NOT NULL,
    city          text        NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

INSERT INTO merchants (merchant_id, merchant_name, category, city)
SELECT g,
       '商户-' || lpad(g::text, 5, '0'),
       CASE WHEN g <= 100 THEN 'A' ELSE (ARRAY['B','C','D','E'])[1 + (g % 4)] END,
       (ARRAY['杭州','上海','北京','深圳','广州','成都','南京','武汉','苏州','西安'])[1 + (g % 10)]
FROM generate_series(1, 5000) AS g;

CREATE TABLE settlement_orders (
    order_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_no    text          NOT NULL,
    merchant_id integer       NOT NULL REFERENCES merchants (merchant_id),
    amount      numeric(12,2) NOT NULL,
    fee         numeric(12,2) NOT NULL,
    status      text          NOT NULL DEFAULT 'SETTLED',
    settle_date date          NOT NULL,
    created_at  timestamptz   NOT NULL DEFAULT now()
);

-- 类别 A 商户（1..100）的 70 万单
INSERT INTO settlement_orders (order_no, merchant_id, amount, fee, status, settle_date)
SELECT 'SO' || lpad(g::text, 10, '0'),
       1 + (random() * 99)::int,
       a.amount,
       round(a.amount * 0.006, 2),
       'SETTLED',
       DATE '2026-08-01' + (random() * 104)::int
FROM generate_series(1, 700000) AS g
CROSS JOIN LATERAL (SELECT round((1 + random() * 499)::numeric, 2) AS amount) AS a;

-- 其余商户（101..5000）的 30 万单
INSERT INTO settlement_orders (order_no, merchant_id, amount, fee, status, settle_date)
SELECT 'SO' || lpad((g + 700000)::text, 10, '0'),
       101 + (random() * 4899)::int,
       a.amount,
       round(a.amount * 0.006, 2),
       'SETTLED',
       DATE '2026-08-01' + (random() * 104)::int
FROM generate_series(1, 300000) AS g
CROSS JOIN LATERAL (SELECT round((1 + random() * 499)::numeric, 2) AS amount) AS a;

CREATE INDEX idx_orders_merchant   ON settlement_orders (merchant_id);
CREATE INDEX idx_orders_settle_date ON settlement_orders (settle_date);

-- 对账演示数据：2026-11-20 的确定性订单（在随机数据日期范围之外，互不干扰），
-- 与网页可下载的样例渠道 CSV（/static/samples/channel_2026-11-20.csv）对应，
-- 用于演示「渠道独有 / 库内独有 / 字段不符 / 同号冲突」四类差异。
INSERT INTO settlement_orders (order_no, merchant_id, amount, fee, status, settle_date) VALUES
-- 001..010：与 CSV 完全一致（10 单一致）
('CH20261120-001', 11, 120.00, 0.72, 'SETTLED', DATE '2026-11-20'),
('CH20261120-002', 12,  88.50, 0.53, 'SETTLED', DATE '2026-11-20'),
('CH20261120-003', 13,  45.00, 0.27, 'SETTLED', DATE '2026-11-20'),
('CH20261120-004', 14, 230.00, 1.38, 'SETTLED', DATE '2026-11-20'),
('CH20261120-005', 15,  19.90, 0.12, 'SETTLED', DATE '2026-11-20'),
('CH20261120-006', 16, 500.00, 3.00, 'SETTLED', DATE '2026-11-20'),
('CH20261120-007', 17,  76.20, 0.46, 'SETTLED', DATE '2026-11-20'),
('CH20261120-008', 18, 134.00, 0.80, 'SETTLED', DATE '2026-11-20'),
('CH20261120-009', 19,  62.50, 0.38, 'SETTLED', DATE '2026-11-20'),
('CH20261120-010', 20,  99.99, 0.60, 'SETTLED', DATE '2026-11-20'),
-- 011..014：CSV 中对应行被改动（金额 / 手续费 / 商户 / 金额+手续费 不符）
('CH20261120-011', 21, 100.00, 0.60, 'SETTLED', DATE '2026-11-20'),
('CH20261120-012', 22, 200.00, 1.20, 'SETTLED', DATE '2026-11-20'),
('CH20261120-013', 23, 150.00, 0.90, 'SETTLED', DATE '2026-11-20'),
('CH20261120-014', 25, 300.00, 1.80, 'SETTLED', DATE '2026-11-20'),
-- 015..016：库内独有（CSV 没有这两单）
('CH20261120-015', 26,  66.00, 0.40, 'SETTLED', DATE '2026-11-20'),
('CH20261120-016', 27,  77.00, 0.46, 'SETTLED', DATE '2026-11-20'),
-- 017：库内同号重复（两行金额不同）-> 冲突，不做配对
('CH20261120-017', 28,  50.00, 0.30, 'SETTLED', DATE '2026-11-20'),
('CH20261120-017', 28,  55.00, 0.33, 'SETTLED', DATE '2026-11-20'),
-- 018：CSV 侧同号重复 -> 冲突
('CH20261120-018', 29,  90.00, 0.54, 'SETTLED', DATE '2026-11-20');
-- 另：CSV 中 CH20261120-101 / -102 库内没有 -> 渠道独有

ANALYZE merchants;
ANALYZE settlement_orders;
