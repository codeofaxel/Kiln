import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';

export default defineConfig({
  site: 'https://kiln3d.com',
  trailingSlash: 'never',
  // /whitepaper was retired but stayed in Google's index; 301 the dead link
  // to the docs so click-throughs land somewhere useful instead of a 404.
  redirects: {
    '/whitepaper': { status: 301, destination: '/docs' },
  },
  integrations: [sitemap()],
  markdown: {
    shikiConfig: {
      theme: 'github-dark',
    },
  },
});
