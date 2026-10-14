import {defineConfig} from 'vite';
export default defineConfig({build:{lib:{entry:'app.js',formats:['cjs'],fileName:()=> 'client.cjs'}}});
