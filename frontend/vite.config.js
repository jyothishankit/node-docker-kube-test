import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    // `npm run dev` proxies API calls to a locally running backend.
    proxy: { '/api': process.env.API_URL || 'http://localhost:5000' },
  },
});
