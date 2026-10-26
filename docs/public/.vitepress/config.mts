import { defineConfig } from 'vitepress'

export default defineConfig({
  lang: 'ru-RU',
  title: 'Undercore Orchestrator',
  description: 'Документация самостоятельного сервиса управления VPN-подключениями',
  base: '/orchestrator/',
  // The VitePress root is docs/public. Sibling docs/internal is never a source.
  lastUpdated: true,
  cleanUrls: false,
  themeConfig: {
    siteTitle: 'Undercore · Orchestrator',
    nav: [
      { text: 'Руководство', link: '/quickstart' },
      { text: 'API', link: '/api' },
      { text: 'VPN Node', link: 'https://github.com/UndercoreCo/vpn-node' },
    ],
    sidebar: [
      { text: 'Начало работы', items: [
        { text: 'Обзор', link: '/' },
        { text: 'Матрица поддержки', link: '/support' },
        { text: 'Установка', link: '/quickstart' },
        { text: 'Интеграция backend', link: '/integration' },
      ] },
      { text: 'Справочник', items: [
        { text: 'HTTP API', link: '/api' },
        { text: 'Ошибки и повторы', link: '/errors' },
        { text: 'Конфигурация', link: '/configuration' },
        { text: 'Контракт VPN-узла', link: '/node-agent' },
        { text: 'TrustTunnel: эксперимент', link: '/trusttunnel' },
        { text: 'Эксплуатация', link: '/operations' },
        { text: 'Панель оркестратора', link: '/panel' },
        { text: 'Диагностика и метрики', link: '/observability' },
        { text: 'Нагрузка и сроки', link: '/runtime-limits' },
      ] },
      { text: 'Разработка', items: [
        { text: 'Архитектура', link: '/architecture' },
        { text: 'Жизненный цикл', link: '/lifecycle' },
        { text: 'Совместимость', link: '/compatibility' },
        { text: 'Протокольные драйверы', link: '/drivers' },
        { text: 'Участие в проекте', link: '/contributing' },
        { text: 'План развития', link: '/roadmap' },
        { text: 'Безопасность', link: '/security' },
        { text: 'Проверки выпуска', link: '/release-security' },
        { text: 'Лицензия', link: '/licensing' },
      ] },
    ],
    search: { provider: 'local' },
    socialLinks: [{ icon: 'github', link: 'https://github.com/UndercoreCo/orchestrator' }],
    editLink: {
      pattern: 'https://github.com/UndercoreCo/orchestrator/edit/main/docs/public/:path',
      text: 'Редактировать на GitHub',
    },
    outline: { label: 'На этой странице', level: [2, 3] },
    docFooter: { prev: 'Предыдущая страница', next: 'Следующая страница' },
    lastUpdated: { text: 'Обновлено' },
    darkModeSwitchLabel: 'Тема',
    sidebarMenuLabel: 'Меню',
    returnToTopLabel: 'Наверх',
    footer: {
      message: 'Исходный код и документация — MIT',
      copyright: '© 2026 Undercore contributors',
    },
  },
})
