-- 首次初始化数据库时执行。扩展也会在迁移中 IF NOT EXISTS 再声明一次。
CREATE EXTENSION IF NOT EXISTS vector;
-- 集成测试使用独立库，避免污染开发数据
CREATE DATABASE twinfix_test OWNER twinfix;
\connect twinfix_test
CREATE EXTENSION IF NOT EXISTS vector;
