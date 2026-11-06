import { fileURLToPath } from 'node:url'
import * as filesystem from '@deepseek-ai/dsh-skill-filesystem'

export const name = 'defeature-skills'
export const inject = ['skills']

export function apply(ctx, config = {}) {
  ctx.plugin(filesystem, {
    providerName: 'defeature',
    includeDefaultRoots: config.includeDefaultRoots === true,
    customSkillDirs: [fileURLToPath(new URL('../skills', import.meta.url))],
  })
}
