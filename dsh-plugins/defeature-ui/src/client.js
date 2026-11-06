import { CadWorkbench } from './CadWorkbench.js'

export const inject = ['slots', 'sessions']

export function apply(ctx) {
  ctx.slots.inject('conversation.view', () =>
    ctx.slots.register(
      {
        name: 'conversation.view',
        id: 'defeature-cad',
        order: 20,
        label: 'CAD 工作台',
      },
      CadWorkbench,
    ),
  )
}
