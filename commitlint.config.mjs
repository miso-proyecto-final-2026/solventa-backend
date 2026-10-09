export default {
  extends: ['@commitlint/config-conventional'],
  rules: {
    'type-enum': [
      2,
      'always',
      ['feat', 'fix', 'docs', 'test', 'refactor', 'ci', 'build', 'chore'],
    ],
    'scope-empty': [1, 'never'],
    'subject-case': [0],
    'header-max-length': [2, 'always', 100],
  },
};
