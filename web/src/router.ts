import { createRouter, createWebHistory } from 'vue-router'
import DashboardView from './views/DashboardView.vue'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'dashboard', component: DashboardView, meta: { title: 'Dashboard' } },
    {
      path: '/groups',
      name: 'groups',
      component: () => import('./views/GroupsView.vue'),
      meta: { title: 'Duplicate groups' },
    },
    {
      path: '/groups/:id(\\d+)',
      name: 'group',
      component: () => import('./views/GroupDetailView.vue'),
      props: (route) => ({ id: Number(route.params.id) }),
      meta: { title: 'Duplicate group' },
    },
    {
      path: '/compare',
      name: 'compare',
      component: () => import('./views/CompareView.vue'),
      meta: { title: 'Compare matchers' },
    },
    {
      path: '/runs/:id(\\d+)',
      name: 'run',
      component: () => import('./views/RunView.vue'),
      props: (route) => ({ id: Number(route.params.id) }),
      meta: { title: 'Run' },
    },
    {
      path: '/actions',
      name: 'actions',
      component: () => import('./views/ActionsView.vue'),
      meta: { title: 'Action log' },
    },
    {
      path: '/config',
      name: 'config',
      component: () => import('./views/ConfigView.vue'),
      meta: { title: 'Configuration' },
    },
    { path: '/:pathMatch(.*)*', name: 'not-found', redirect: '/' },
  ],
  scrollBehavior(to, _from, savedPosition) {
    if (savedPosition) return savedPosition
    if (to.hash) return { el: to.hash }
    return { top: 0 }
  },
})

router.afterEach((to) => {
  const title = to.meta.title as string | undefined
  document.title = title ? `${title} · py-image-dedup` : 'py-image-dedup'
})
