<template>
  <div class="app-shell">
    <aside class="app-side">
      <h1 class="app-title">Web语义抽取与内容理解中心</h1>
      <nav class="nav-list">
        <RouterLink v-for="item in navItems" :key="item.path" :to="item.path" class="nav-item">
          {{ item.label }}
        </RouterLink>
      </nav>
    </aside>
    <main class="app-main">
      <header class="app-head">
        <span class="head-desc">面向Web结构化数据采集场景，提供页面DOM解析、实体识别、关系抽取、内容分类和知识图谱构建的语义抽取平台</span>
        <span class="head-user">当前值班：数据工程组</span>
      </header>
      <RouterView />
    </main>
  </div>
</template>

<script setup lang="ts">
const navItems = [{ label: "运营概览", path: "/" }, { label: "槽位迁移", path: "/slot-migration" }, { label: "DOM解析", path: "/dom_parser" }, { label: "实体抽取", path: "/entity_extract" }, { label: "关系图谱", path: "/relation_graph" }, { label: "内容分类", path: "/content_classifier" }, { label: "模板规则", path: "/template_rule" }, { label: "文本分块", path: "/text_segment" }, { label: "情感标注", path: "/sentiment_tag" }, { label: "Schema对齐", path: "/schema_align" }, { label: "种子调度", path: "/feed_scheduler" }, { label: "质量检查", path: "/quality_check" }, { label: "探针选择", path: "/probe_selector" }, { label: "页面快照", path: "/page_snapshot" }, { label: "抽取指标", path: "/extract_metric" }, { label: "语义结果", path: "/nlu_result" }, { label: "知识链接", path: "/knowledge_link" }, { label: "输出目标", path: "/output_target" }, { label: "抽取工坊", path: "/extract_studio" }, { label: "聚合视图", path: "/aggregate_view" }]
</script>
