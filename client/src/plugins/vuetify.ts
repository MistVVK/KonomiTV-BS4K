import 'vuetify/styles';
import '@/styles/materialdesignicons.min.css';
import { createVuetify } from 'vuetify';
import { ja } from 'vuetify/locale';

import { KONOMITV_BS4K_THEMES } from '@/themes';


const vuetify = createVuetify({
    locale: {
        locale: 'ja',
        fallback: 'ja',
        messages: { ja },
    },
    theme: {
        defaultTheme: 'KonomiClassic',
        themes: KONOMITV_BS4K_THEMES,
    },
});

export default vuetify;
