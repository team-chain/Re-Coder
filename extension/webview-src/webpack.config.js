/**
 * ReCoder Webview Webpack Configuration
 *
 * 빌드 파이프라인:
 *   1. TypeScript → ts-loader 로 컴파일
 *   2. CSS → postcss-loader (tailwind + autoprefixer) → css-loader → style-loader
 *      style-loader 는 빌드 결과 JS 가 런타임에 <style> 태그를 주입한다.
 *      VSCode webview 는 별도 CSS 파일 로딩이 까다로워서 JS 인라인 주입이 안전.
 */
const path = require('path');

module.exports = {
  entry: './index.tsx',
  output: {
    path: path.resolve(__dirname, '..', 'out', 'webview'),
    filename: 'webview.js',
  },
  resolve: {
    extensions: ['.tsx', '.ts', '.js'],
  },
  module: {
    rules: [
      {
        test: /\.tsx?$/,
        use: {
          loader: 'ts-loader',
          options: {
            configFile: path.resolve(__dirname, 'tsconfig.json'),
          },
        },
        exclude: /node_modules/,
      },
      {
        // Tailwind CSS pipeline
        test: /\.css$/,
        use: [
          'style-loader',
          {
            loader: 'css-loader',
            options: { importLoaders: 1 },
          },
          {
            loader: 'postcss-loader',
            options: {
              postcssOptions: {
                config: path.resolve(__dirname, 'postcss.config.js'),
              },
            },
          },
        ],
      },
    ],
  },
  performance: { hints: false },
};
