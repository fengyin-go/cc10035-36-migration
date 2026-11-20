import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import SlotMigrationView from '@/views/SlotMigrationView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/slot-migration', name: 'slot-migration', component: SlotMigrationView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
