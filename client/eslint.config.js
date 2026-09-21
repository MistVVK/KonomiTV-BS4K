const { resolve } = require('node:path');

const { includeIgnoreFile } = require('@eslint/compat');
const js = require('@eslint/js');
const stylistic = require('@stylistic/eslint-plugin');
const { withVueTs, vueTsConfigs } = require('@vue/eslint-config-typescript');
const importPlugin = require('eslint-plugin-import');
const unusedImports = require('eslint-plugin-unused-imports');
const vue = require('eslint-plugin-vue');
const vuetify = require('eslint-plugin-vuetify');
const globals = require('globals');


module.exports = withVueTs(
    // 旧 --ignore-path と同じ .gitignore を正本にし、生成物・ローカル設定を検査対象へ戻さない
    includeIgnoreFile(resolve(__dirname, '.gitignore')),
    {
        ignores: ['**/.*', '**/dist/**', 'public/vendor/librespeed/**'],
    },
    vue.configs['flat/essential'],
    vuetify.configs['flat/recommended'],
    js.configs.recommended,
    // 旧 @vue/eslint-config-typescript の既定と同じく、型チェックと重複する基本ルールだけ調整する
    vueTsConfigs.base,
    vueTsConfigs.eslintRecommended,
    {
        // 旧設定では Vue SFC にも有効だった JavaScript の基本検査を維持する
        files: ['**/*.vue'],
        rules: js.configs.recommended.rules,
    },
    {
        languageOptions: {
            globals: globals.node,
        },
        linterOptions: {
            // ESLint 9 の既定変更で、既存の抑制コメントを --fix が自動削除しないようにする
            reportUnusedDisableDirectives: 'off',
        },
        plugins: {
            '@stylistic': stylistic,
            import: importPlugin,
            'unused-imports': unusedImports,
        },
        rules: {
            '@stylistic/indent': ['error', 4, {SwitchCase: 1}],
            '@stylistic/quotes': ['error', 'single'],
            '@stylistic/semi': ['error', 'always'],
            '@stylistic/no-extra-semi': 'error',
            '@stylistic/no-mixed-spaces-and-tabs': 'error',
            '@typescript-eslint/no-unused-vars': 'off',
            'import/order': ['warn', {
                alphabetize: {order: 'asc', caseInsensitive: true},
                groups: ['builtin', 'external', 'internal', 'parent', 'sibling', 'index', 'object', 'type'],
                'newlines-between': 'always',
                pathGroupsExcludedImportTypes: ['builtin'],
            }],
            'unused-imports/no-unused-imports': 'warn',
            'vue/multi-word-component-names': 'off',
            'vue/no-reserved-component-names': 'off',
            'no-constant-condition': ['error', {checkLoops: false}],
            'no-class-assign': 'error',
            'no-inner-declarations': ['error', 'functions', {blockScopedFunctions: 'disallow'}],
            'no-unused-vars': 'off',
            'no-with': 'error',
        },
    },
    {
        files: ['**/*.{ts,cts,mts,tsx,vue}'],
        rules: {
            'no-undef': 'off',
        },
    },
    {
        files: ['babel.config.js', 'eslint.config.js'],
        languageOptions: {
            sourceType: 'commonjs',
        },
    },
    {
        files: ['scripts/generate-license-document.mjs'],
        rules: {
            // 配布するライセンス文書への制御文字混入を拒否する検出パターンを維持する
            'no-control-regex': 'off',
        },
    },
    {
        files: ['src/components/Videos/RecordedProgram.vue'],
        rules: {
            // 再解析後は共有レコードの内容を更新する。prop 自体の参照置換は禁止したままにする
            'vue/no-mutating-props': ['error', {shallowOnly: true}],
        },
    },
);
