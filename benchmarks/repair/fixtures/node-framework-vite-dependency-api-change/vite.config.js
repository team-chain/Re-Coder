import {defineConfig} from 'vite/dist/node';
export default defineConfig({build:{lib:{entry:'app.js',formats:['cjs'],fileName:()=> 'client.cjs'}}});
