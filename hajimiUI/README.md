# hajimiUI

This template should help get you started developing with Vue 3 in Vite.

## Recommended IDE Setup

[VSCode](https://code.visualstudio.com/) + [Volar](https://marketplace.visualstudio.com/items?itemName=Vue.volar) (and disable Vetur).

## Customize configuration

See [Vite Configuration Reference](https://vite.dev/config/).

## Project Setup

```sh
npm install
```

### Compile and Hot-Reload for Development

```sh
npm run dev
```

### Build for Production (serves the backend)

```sh
npm run build
```

`build` runs `build.js`, which:

1. runs `vite build` with `outDir` pointing at `../app/templates/assets`;
2. deletes the stray `index.html` vite emits into the assets dir;
3. renames every asset to a random 32-hex name (anti-fingerprint);
4. writes a hand-rolled `../app/templates/index.html` referencing the new names.

**The served UI is the committed build output in `app/templates/`** — after
changing anything in `src/`, you MUST run `npm run build` and commit the
updated `app/templates/` files, otherwise the deployment keeps serving the
old UI. (`npm run build:app` is an alias kept for backwards compatibility;
never run bare `vite build` — it wipes the assets dir with fixed-name output
and leaves `index.html` pointing at deleted hashed files.)
