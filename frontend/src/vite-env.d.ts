/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_APPLICATION_BUILD_ID?: string;
  readonly VITE_CLIENT_COMPATIBILITY_VERSION?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
