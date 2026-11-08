"""Ready-made hashtag library for captions (app/services/hashtags.py).

African and diaspora culture first, in English, French, Portuguese and Swahili.
Edit freely: tags are plain words (letters, digits, _), no '#'."""

_EN, _FR, _PT, _SW = "en", "fr", "pt", "sw"


def _tags(lang, words):
    return [{"tag": w, "lang": lang} for w in words.split()]


LIBRARY = [
    {"category": "Music & dance", "tags":
        _tags(_EN, "Afrobeats Amapiano AfroHouse Afropop Bongo BongoFlava Gengetone Gqom Kwaito Highlife Hiplife "
                   "Kizomba Kuduro Azonto Shaku Zanku Legwork Gwara Ndombolo Rumba Soukous Coupe Decale Makossa "
                   "Mbalax Afrojuju Fuji Juju Apala Taarab Benga KadongoKamu LugaFlow Kidandali AfroSoul AfroTrap "
                   "Drill AfroDrill DanceChallenge DanceVideo NewMusic Freestyle Cover Acoustic Choir Drummers "
                   "LiveBand StreetDance Choreography")
        + _tags(_FR, "MusiqueAfricaine DanseAfricaine CoupéDécalé RumbaCongolaise Ndombolo Afrobeat RapFrançais "
                     "Concert Chanson")
        + _tags(_PT, "Kizomba Semba Kuduro Funk Pagode Samba Axé Forró Afrobrasil BailePerfeito Dança")
        + _tags(_SW, "Muziki Bongo Singeli Ngoma Densi Taarab Wimbo")},
    {"category": "Comedy", "tags":
        _tags(_EN, "Comedy Skit Skits Funny FunnyVideos Prank Pranks Relatable AfricanParents AfricanMoms "
                   "AfricanComedy NaijaComedy KenyanComedy UgandanComedy Parody Roast Bloopers Jokes Memes "
                   "StandUp Laugh LOL")
        + _tags(_FR, "Humour Drôle Blague Sketch Rire ComédieAfricaine")
        + _tags(_PT, "Humor Comédia Engraçado Zoeira Pegadinha Risos")
        + _tags(_SW, "Vichekesho Utani Kichekesho Mzaha")},
    {"category": "Food", "tags":
        _tags(_EN, "AfricanFood Jollof JollofRice Suya Egusi Fufu Banku Waakye Kelewele Chapati Rolex Luwombo "
                   "Matooke Ugali Nyamachoma Pilau Injera Bunnychow Braai Bobotie Kapenta Nshima Thieboudienne "
                   "Yassa Attieke Alloco Ndole Moambe Pondu StreetFood Foodie Recipe Cooking HomeCooking "
                   "WhatIEatInADay FoodTour")
        + _tags(_FR, "CuisineAfricaine Recette Cuisine Gastronomie Délicieux Attiéké Poulet Mafé")
        + _tags(_PT, "Comida Receita Culinária Feijoada Moqueca Acarajé Muamba Calulu ComidaDeRua")
        + _tags(_SW, "Chakula Mapishi Pilau Chapati Mandazi Mishkaki Ugali Wali")},
    {"category": "Fashion & beauty", "tags":
        _tags(_EN, "AfricanFashion Ankara Kitenge Kente Dashiki Agbada Boubou Kaftan Gomesi Kanzu Shweshwe "
                   "Headwrap Gele BraidStyles Braids Locs NaturalHair Wigs Makeup MakeupTutorial SkinCare "
                   "BlackGirlMagic MelaninPoppin OOTD StreetStyle Thrift Sneakers Tailor Designer Fashion "
                   "Style Beauty GRWM")
        + _tags(_FR, "ModeAfricaine Pagne Wax Coiffure Tresses Maquillage Beauté Tenue Sapeur LaSape")
        + _tags(_PT, "ModaAfricana Moda Estilo Tranças CabeloNatural Maquiagem Beleza Look")
        + _tags(_SW, "Mitindo Urembo Kanga Kitenge Mavazi Nywele")},
    {"category": "Culture", "tags":
        _tags(_EN, "AfricanCulture Tradition Heritage Proverbs Storytelling Folklore Wedding AfricanWedding "
                   "Kwanjula Introduction Lobola Ceremony Festival Masquerade Griot History AfricanHistory "
                   "Diaspora AfroDiaspora PanAfrican Ubuntu Language Luganda Swahili Yoruba Igbo Hausa Twi Zulu "
                   "Xhosa Amharic Lingala Wolof Kinyarwanda")
        + _tags(_FR, "CultureAfricaine Tradition Patrimoine Proverbe Conte Mariage Diaspora Histoire Lingala")
        + _tags(_PT, "CulturaAfricana AfroBrasileiro Capoeira Candomblé Quilombo Maracatu Tradição Diáspora")
        + _tags(_SW, "Utamaduni Mila Harusi Historia Lugha Kiswahili Methali")},
    {"category": "Places", "tags":
        _tags(_EN, "Africa Uganda Kampala Entebbe Jinja Gulu Mbarara Kenya Nairobi Mombasa Kisumu Tanzania "
                   "DarEsSalaam Zanzibar Arusha Rwanda Kigali Burundi Nigeria Lagos Abuja PortHarcourt Ibadan "
                   "Ghana Accra Kumasi SouthAfrica Johannesburg CapeTown Durban Ethiopia AddisAbaba Zambia "
                   "Lusaka Zimbabwe Harare Malawi Botswana Namibia Egypt Cairo Morocco Sudan SouthSudan "
                   "Somalia Liberia SierraLeone Gambia Cameroon Douala Yaounde Jamaica Kingston Haiti "
                   "Trinidad Barbados Brazil Salvador Bahia RioDeJaneiro Colombia Cartagena London Atlanta "
                   "Houston NewYork Toronto Paris Brussels")
        + _tags(_FR, "Afrique Congo RDC Kinshasa Lubumbashi Goma Brazzaville Sénégal Dakar CôteDIvoire Abidjan "
                     "Mali Bamako BurkinaFaso Ouagadougou Guinée Conakry Bénin Cotonou Togo Lomé Niger Gabon "
                     "Libreville Tchad Madagascar Antananarivo Haïti Martinique Guadeloupe Cameroun Yaoundé")
        + _tags(_PT, "Angola Luanda Moçambique Maputo CaboVerde Praia GuinéBissau SãoTomé Brasil Salvador "
                     "Bahia SãoPaulo RioDeJaneiro Recife Nordeste")
        + _tags(_SW, "Afrika Tanzania Kenya Uganda Dodoma Mwanza Unguja Pwani")},
    {"category": "Sports", "tags":
        _tags(_EN, "Football Soccer AFCON SuperEagles BlackStars Harambee UgandaCranes Bafana Chipolopolo "
                   "Basketball BAL Athletics Running Marathon Boxing Rugby Cricket Netball Volleyball "
                   "Skills FootballSkills Goals Fitness Workout Gym HomeWorkout Yoga")
        + _tags(_FR, "Foot Football Léopards LionsIndomptables Éléphants Sport Musculation Fitness")
        + _tags(_PT, "Futebol Palancas Mambas Capoeira Esporte Treino Academia")
        + _tags(_SW, "Mpira Michezo Mazoezi Riadha TaifaStars")},
    {"category": "Faith", "tags":
        _tags(_EN, "Gospel GospelMusic Worship Praise Prayer Church Sermon Bible Jesus Faith Blessed Muslim "
                   "Ramadan Eid Quran Islam Jummah Testimony Choir Hymns")
        + _tags(_FR, "Gospel Louange Adoration Prière Église Foi Dieu Ramadan")
        + _tags(_PT, "Gospel Louvor Adoração Oração Igreja Fé Deus")
        + _tags(_SW, "Injili Sifa Maombi Kanisa Imani Mungu Ramadhani")},
    {"category": "Business", "tags":
        _tags(_EN, "SmallBusiness Entrepreneur Hustle SideHustle Startup MadeInAfrica BuyAfrican AfricanBrands "
                   "Farming Agribusiness Poultry RealEstate Investing Money MobileMoney Fintech Tech "
                   "AfricanTech Coding Marketing Branding Tips CareerTips")
        + _tags(_FR, "Entrepreneur Business Commerce Agriculture Élevage Argent Investissement Tech")
        + _tags(_PT, "Empreendedor Negócios Agricultura Dinheiro Investimento Tecnologia")
        + _tags(_SW, "Biashara Ujasiriamali Kilimo Ufugaji Pesa Teknolojia")},
    {"category": "Wano TV", "tags":
        _tags(_EN, "WanoTV WanoOriginal Documentary AfricanFilm Nollywood Kannywood Ugawood Riverwood "
                   "ShortFilm Series Docuseries BehindTheScenes Filmmaker Filmmaking Cinematography Trailer "
                   "MovieReview StoryTime WanoAfrica Wano OndoreMedia")
        + _tags(_FR, "Documentaire FilmAfricain Cinéma Série CourtMétrage Nollywood")
        + _tags(_PT, "Documentário FilmeAfricano Cinema Série CurtaMetragem")
        + _tags(_SW, "Filamu Tamthilia Bongomovie")},
    {"category": "Lifestyle", "tags":
        _tags(_EN, "DayInMyLife Vlog Travel TravelAfrica Explore Nature Safari Wildlife Beach Sunset CityLife "
                   "VillageLife Family Couple Love Relationship Parenting Kids Motivation Inspiration "
                   "Education Learn Study Student CampusLife Graduation Art Artist Painting Photography "
                   "Pets Cars Bodaboda Matatu")
        + _tags(_FR, "Vlog Voyage Nature Famille Amour Motivation Étudiant École Art Photographie")
        + _tags(_PT, "Vlog Viagem Natureza Família Amor Motivação Estudante Escola Arte Fotografia")
        + _tags(_SW, "Safari Maisha Familia Upendo Motisha Elimu Shule Sanaa")},
]

# Prompts to spark a video: (text, hashtag), by language.
IDEAS = {
    "en": [
        ("Show your city at night", "CityNights"),
        ("Cook your favourite home dish in 60 seconds", "HomeCooking"),
        ("Teach us one word in your language", "LearnMyLanguage"),
        ("What your mum says when you come home late", "AfricanParents"),
        ("Your best dance move to this week's top song", "DanceChallenge"),
        ("Outfit of the day in African print", "AfricanFashion"),
        ("A day in your life", "DayInMyLife"),
        ("Tell a proverb from your grandmother", "Proverbs"),
        ("Your hustle: show how you make money", "SideHustle"),
        ("Behind the scenes of something you're making", "BehindTheScenes"),
    ],
    "fr": [
        ("Montre ta ville la nuit", "VilleLaNuit"),
        ("Ton plat préféré en 60 secondes", "CuisineAfricaine"),
        ("Apprends-nous un mot dans ta langue", "MaLangue"),
        ("Ce que dit ta maman quand tu rentres tard", "ParentsAfricains"),
        ("Ton meilleur pas de danse", "DanseAfricaine"),
        ("Ta tenue du jour en pagne", "ModeAfricaine"),
        ("Une journée dans ma vie", "MaJournée"),
        ("Un proverbe de ta grand-mère", "Proverbe"),
    ],
    "pt": [
        ("Mostra a tua cidade à noite", "CidadeÀNoite"),
        ("O teu prato favorito em 60 segundos", "ComidaCaseira"),
        ("Ensina-nos uma palavra na tua língua", "MinhaLíngua"),
        ("O que a tua mãe diz quando chegas tarde", "MãeAfricana"),
        ("O teu melhor passo de dança", "Dança"),
        ("O teu look do dia", "ModaAfricana"),
        ("Um dia na minha vida", "UmDiaNaMinhaVida"),
        ("Um provérbio da tua avó", "Provérbio"),
    ],
    "sw": [
        ("Onyesha mji wako usiku", "MjiUsiku"),
        ("Pika chakula unachokipenda kwa sekunde 60", "Mapishi"),
        ("Tufundishe neno moja la lugha yako", "LughaYangu"),
        ("Mama yako anasema nini ukirudi usiku", "WazaziWaKiafrika"),
        ("Hatua yako bora ya densi", "Densi"),
        ("Vazi lako la leo", "Mitindo"),
        ("Siku moja maishani mwangu", "SikuYangu"),
        ("Methali kutoka kwa bibi yako", "Methali"),
    ],
}
