import streamlit as st
import textwrap
import google.generativeai as genai
import requests
# Set Google API Key
genai.configure(api_key='AIzaSyCBlC0Gz2acJMdNNzfbrXpuJABkOlmkDns')
# Define function to convert text to markdown
def to_markdown(text):
   text = text.replace('•', ' *')
   return textwrap.indent(text, '> ')
# Define Streamlit app
def main():
   st.title("e2open Trade Compliance Demistifyer")

   
   # Select model
   selected_model = 'gemini-pro'
   # Generate content
   text_input = st.text_area("Enter Text:")


   st.subheader("Select output language")
   options = ["English", "French", "Russian", "Spanish"]
   selected_option = st.selectbox("Choose an option:", options)


   if selected_option == 'English':
      target_language = 'en'  # Default to English
   if selected_option == 'French':
       target_language = 'fr'
   elif selected_option == 'Russian':
       target_language = 'ru'
   elif selected_option == 'Spanish':
       target_language = 'es'


   
   if st.button("Generate Content"):
      model = genai.GenerativeModel(selected_model)
      response = model.generate_content(text_input)
      print(response.text)

      if target_language == 'en':
         st.markdown(to_markdown(response.text))
      else:   
      

         url = "https://translation.googleapis.com/language/translate/v2"

         querystring = {"key":"AIzaSyAZuEHsxe7s_swEIkBKJETznel05naET_8"}

         payload = {
            "q": "",
            "source": "",
            "target": "",
            "format": "text"
         }
         payload['q']=response.text
         payload['source']='en'
         payload['target']=target_language
         headers = {"Content-Type": "application/json"}

         response = requests.request("POST", url, json=payload, headers=headers, params=querystring)


         data = response.json()


         response1 = data['data']['translations'][0]['translatedText']
         print(response1)





         st.markdown(to_markdown(response1))
# Run Streamlit app
if __name__ == "__main__":
   main()