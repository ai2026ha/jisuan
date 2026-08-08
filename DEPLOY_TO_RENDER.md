# Render 部署说明

## 一键部署前提

1. 注册 Render。
2. 将整个项目上传到 GitHub 私有仓库。
3. 在 Render 选择 **New → Blueprint**，连接这个 GitHub 仓库。
4. Render 会读取根目录的 `render.yaml`，自动创建 Web Service + PostgreSQL。
5. 部署完成后，Render 会提供 `https://xxxx.onrender.com` 地址。

## 默认登录

用户名：`admin`
密码：`admin123`

首次登录后建议马上修改管理员密码（当前版本暂未加入改密页面）。

## 重要

Render 免费 Web Service 会在闲置后休眠；免费 PostgreSQL 数据库目前会在创建 30 天后到期。因此这个方案适合测试，不适合长期保存重要业务数据。长期使用时应把数据库升级为付费实例或迁移到长期免费的数据库服务。
