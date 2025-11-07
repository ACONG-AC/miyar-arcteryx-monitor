# MIYAR Arc'teryx Monitor

监控 https://store.miyaradventures.com/ 的 Arc'teryx 商品（变体级）：
- 上新（新商品 / 新变体）
- 价格变化
- 库存状态变化（缺货/到货）
- 库存数量增加（若主题暴露 inventory_quantity）

并逐条推送到 Discord（Webhook）。

## 使用
1. Fork 本仓库或新建空仓库，放入本目录所有文件。
2. 仓库 → Settings → Secrets and variables → Actions：
   - 新建 `DISCORD_WEBHOOK`，值为你的 Webhook URL。
3. 启用 GitHub Actions；默认每 **21 分钟**跑一次，也可手动 `Run workflow`。

## 备注
- 如果 `/products.json` 被禁用，脚本自动用 `sitemap_products_*.xml` + `/products/<handle>.js` 回退抓取。
- `inventory_quantity` 是否可见取决于主题；若不可见，将仅基于 `available` 做到货/缺货监控。
- 变更结果写入 `snapshot.json`，用于下次 diff 与去重。
