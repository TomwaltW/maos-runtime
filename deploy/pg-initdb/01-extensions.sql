-- 本机 compose 起 pgvector 时自动装扩展，省掉「起完还要手动连进去跑一条 DDL」那步。
--
-- 官方 postgres 镜像（pgvector/pgvector:pg16 基于它）会在**初始化数据目录时**
-- 按文件名顺序执行 /docker-entrypoint-initdb.d/ 下的 *.sql 与 *.sh。
--
-- 🔴 只在数据目录为空时跑。已经起过一次、pgdata 卷里有数据的，这个文件不会被执行 ——
--    这是官方镜像的行为，不是配置写错了。要让它生效：
--        docker compose --profile pg down -v      # -v 才会删掉 pgdata 卷
--        docker compose --profile pg up -d
--    「改了 initdb 脚本却没生效」是这套机制最常见的坑，且它不报错、只是安静地跳过。
--
-- 云上实例不走这条路：那边装扩展要高权限账号，见 deploy/polardb-live.md §1.3。

CREATE EXTENSION IF NOT EXISTS vector;
