import os
import asyncio
from datetime import datetime, timezone
from typing import List, Optional, Dict
import discord
from discord import ui, SelectOption
from discord.ext import commands
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import IndexModel, TEXT
from beanie import Document, init_beanie
from pydantic import Field
from dotenv import load_dotenv

load_dotenv()

# ======================
# DATABASE MODEL
# ======================
class TypologyEntry(Document):
    title: str
    category: str
    topic: str
    description: str
    author_id: int
    author_name: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    image_url: str = Field(default="")
    reference: str = Field(default="")
    votes: int = Field(default=0)
    voters: List[int] = Field(default_factory=list)
    
    class Settings:
        name = "typology_entries"

# ======================
# DISTINCT VALUE HELPERS
# ======================
async def get_distinct_categories() -> List[str]:
    client = AsyncIOMotorClient(os.getenv("MONGO_URI"))
    db = client[os.getenv("MONGO_DB", "typology_bot")]
    collection = db["typology_entries"]
    return await collection.distinct("category")

async def get_distinct_topics(category: str) -> List[str]:
    client = AsyncIOMotorClient(os.getenv("MONGO_URI"))
    db = client[os.getenv("MONGO_DB", "typology_bot")]
    collection = db["typology_entries"]
    return await collection.distinct("topic", {"category": category})

async def get_distinct_titles() -> List[str]:
    client = AsyncIOMotorClient(os.getenv("MONGO_URI"))
    db = client[os.getenv("MONGO_DB", "typology_bot")]
    collection = db["typology_entries"]
    return await collection.distinct("title")

# ======================
# DATABASE INITIALIZATION WITH MIGRATION
# ======================
async def initialize_database():
    client = AsyncIOMotorClient(os.getenv("MONGO_URI"))
    db = client[os.getenv("MONGO_DB", "typology_bot")]
    
    # Initialize Beanie with new model
    await init_beanie(database=db, document_models=[TypologyEntry])
    
    # Migrate data from old collection if exists
    if "typology_definitions" in await db.list_collection_names():
        old_collection = db["typology_definitions"]
        new_collection = db["typology_entries"]
        
        # Only migrate if new collection is empty
        if await new_collection.count_documents({}) == 0:
            print("🚚 Migrating old definitions to new format...")
            async for doc in old_collection.find():
                # Map old fields to new structure
                new_doc = {
                    "title": doc["term"],
                    "category": "General",
                    "topic": "General",
                    "description": doc["text"],
                    "author_id": doc["author_id"],
                    "author_name": doc["author_name"],
                    "created_at": doc["created_at"],
                    "last_updated": doc["last_updated"],
                    "image_url": "",
                    "reference": doc.get("reference", ""),
                    "votes": doc.get("votes", 0),
                    "voters": doc.get("voters", []),
                }
                await new_collection.insert_one(new_doc)
            
            print(f"✅ Migrated {await old_collection.count_documents({})} entries")
            # Rename instead of delete to keep backup
            await old_collection.rename("typology_definitions_backup")
    
    # Manual index management
    collection = db["typology_entries"]
    existing_indexes = await collection.index_information()
    
    # Define our desired indexes
    desired_indexes = [
        ("title_idx", [("title", 1)]),
        ("category_idx", [("category", 1)]),
        ("topic_idx", [("topic", 1)]),
        ("popularity_idx", [("votes", -1)]),
        ("author_idx", [("author_id", 1)]),
        ("search_idx", [("title", TEXT), ("description", TEXT)])
    ]
    
    for index_name, index_keys in desired_indexes:
        # Check for existing index with same keys but different name
        conflict_exists = False
        for existing_name, existing_info in existing_indexes.items():
            if existing_name == "_id_":
                continue
                
            # Normalize index representations
            existing_keys = [(k, v) for k, v in existing_info["key"]]
            normalized_existing = []
            for key, spec in existing_keys:
                if spec == "text":
                    normalized_existing.append((key, TEXT))
                else:
                    normalized_existing.append((key, int(spec)))
            
            # Compare key sets
            if set(normalized_existing) == set(index_keys) and existing_name != index_name:
                conflict_exists = True
                try:
                    await collection.drop_index(existing_name)
                    print(f"♻️ Dropped conflicting index: {existing_name}")
                    break
                except Exception as e:
                    print(f"⚠️ Failed to drop index {existing_name}: {str(e)}")
        
        # Create index if needed
        if conflict_exists or index_name not in existing_indexes:
            try:
                # Special handling for text index
                if any(spec == TEXT for _, spec in index_keys):
                    # Create text index with proper specification
                    await collection.create_index(
                        [(field, TEXT) for field, spec in index_keys],
                        name=index_name
                    )
                else:
                    await collection.create_index(index_keys, name=index_name)
                print(f"✅ Created index: {index_name}")
            except Exception as e:
                print(f"⚠️ Failed to create index {index_name}: {str(e)}")

# ======================
# HIERARCHICAL UI COMPONENTS
# ======================
class CategorySelect(ui.View):
    def __init__(self, categories: List[str]):
        super().__init__(timeout=60.0)
        self.category = None
        self.categories = categories
        
        # Add select menu
        self.add_item(CategoryDropdown(categories))

class CategoryDropdown(ui.Select):
    def __init__(self, categories: List[str]):
        options = [
            SelectOption(label=cat, value=cat, emoji="📂")
            for cat in categories
        ]
        options.append(SelectOption(label="+ Create New Category", value="__new__", emoji="➕"))
        super().__init__(placeholder="Select a category...", options=options)
        
    async def callback(self, interaction: discord.Interaction):
        self.view.category = self.values[0]
        self.view.stop()
        await interaction.response.defer()

class TopicSelect(ui.View):
    def __init__(self, topics: List[str]):
        super().__init__(timeout=60.0)
        self.topic = None
        self.topics = topics
        
        # Add select menu
        self.add_item(TopicDropdown(topics))

class TopicDropdown(ui.Select):
    def __init__(self, topics: List[str]):
        options = [
            SelectOption(label=topic, value=topic, emoji="📝")
            for topic in topics
        ]
        options.append(SelectOption(label="+ Create New Topic", value="__new__", emoji="➕"))
        super().__init__(placeholder="Select a topic...", options=options)
        
    async def callback(self, interaction: discord.Interaction):
        self.view.topic = self.values[0]
        self.view.stop()
        await interaction.response.defer()

# ======================
# ENTRY VIEW WITH EDIT/MOVE FUNCTIONALITY
# ======================
class EntryView(ui.View):
    def __init__(self, entries: List[TypologyEntry], user_id: int):
        super().__init__(timeout=120.0)
        self.entries = entries
        self.user_id = user_id
        self.page = 0
        self.message = None

    async def update_embed(self, interaction: Optional[discord.Interaction] = None):
        if not self.entries:
            if self.message:
                await self.message.edit(content="❌ No entries found", embed=None, view=None)
            return
            
        entry = self.entries[self.page]
        
        # Fetch author
        try:
            author = await bot.fetch_user(entry.author_id)
        except:
            author = None
        
        embed = discord.Embed(
            title=f"📚 {entry.title}",
            description=entry.description,
            color=0x6A0DAD,
            timestamp=datetime.now(timezone.utc)
        )
        
        # Add hierarchy information
        embed.add_field(
            name="Category",
            value=f"```\n{entry.category}\n```",
            inline=False
        )
        embed.add_field(
            name="Topic",
            value=f"```\n{entry.topic}\n```",
            inline=False
        )
        
        # Add author info
        if author:
            embed.set_author(name=author.display_name, icon_url=author.display_avatar.url)
        else:
            embed.set_author(name="Unknown Author")
        
        # Add reference if exists
        if entry.reference:
            embed.add_field(
                name="Reference",
                value=f"```\n{entry.reference}\n```",
                inline=False
            )
            
        # Add image if available
        if entry.image_url:
            embed.set_image(url=entry.image_url)
            
        # Footer with metadata
        footer_text = [
            f"⭐ Votes: {entry.votes}",
            f"🆔 ID: {entry.id}",
            f"📅 Created: {discord.utils.format_dt(entry.created_at, style='R')}",
            f"🔄 Updated: {discord.utils.format_dt(entry.last_updated, style='R')}"
        ]
        embed.set_footer(text=" • ".join(footer_text))
        
        if interaction:
            await interaction.response.edit_message(embed=embed, view=self)
        elif self.message:
            await self.message.edit(embed=embed, view=self)

    @ui.button(emoji="⬅️", style=discord.ButtonStyle.blurple)
    async def prev_page(self, interaction: discord.Interaction, button: ui.Button):
        self.page = max(0, self.page - 1)
        await self.update_embed(interaction)

    @ui.button(emoji="➡️", style=discord.ButtonStyle.blurple)
    async def next_page(self, interaction: discord.Interaction, button: ui.Button):
        if self.page < len(self.entries) - 1:
            self.page += 1
            await self.update_embed(interaction)

    @ui.button(emoji="⭐", style=discord.ButtonStyle.green)
    async def upvote(self, interaction: discord.Interaction, button: ui.Button):
        entry = self.entries[self.page]
        
        if interaction.user.id in entry.voters:
            await interaction.response.send_message("❌ You've already voted!", ephemeral=True)
            return
            
        entry.voters.append(interaction.user.id)
        entry.votes += 1
        entry.last_updated = datetime.now(timezone.utc)
        await entry.save()
        
        await interaction.response.send_message("✅ Vote recorded!", ephemeral=True)
        await self.update_embed()

    @ui.button(emoji="✏️", style=discord.ButtonStyle.gray)
    async def edit_btn(self, interaction: discord.Interaction, button: ui.Button):
        entry = self.entries[self.page]
        
        if interaction.user.id != entry.author_id:
            await interaction.response.send_message(
                "❌ You can only edit your own entries", 
                ephemeral=True
            )
            return
            
        modal = EditModal(entry)
        await interaction.response.send_modal(modal)

    @ui.button(emoji="🚚", style=discord.ButtonStyle.secondary)
    async def move_btn(self, interaction: discord.Interaction, button: ui.Button):
        entry = self.entries[self.page]
        
        if interaction.user.id != entry.author_id:
            await interaction.response.send_message(
                "❌ You can only move your own entries", 
                ephemeral=True
            )
            return
            
        # Start move process
        await self.start_move_process(interaction, entry)
        
    async def start_move_process(self, interaction: discord.Interaction, entry: TypologyEntry):
        # Step 1: Category Selection
        categories = await get_distinct_categories()
        category_view = CategorySelect(categories)
        category_msg = await interaction.followup.send(
            f"**📂 Select a new category for '{entry.title}'**",
            view=category_view,
            ephemeral=True
        )
        
        # Wait for category selection
        await category_view.wait()
        if not category_view.category:
            await category_msg.edit(content="⏱️ Category selection timed out", view=None)
            return
            
        # Handle new category creation
        if category_view.category == "__new__":
            await category_msg.edit(content="⌛ Waiting for new category...", view=None)
            
            def check(m):
                return m.author == interaction.user and m.channel == interaction.channel
                
            try:
                await interaction.followup.send("Please enter a name for the new category:", ephemeral=True)
                response = await bot.wait_for('message', timeout=60.0, check=check)
                category = response.content.strip()
                await response.delete()
            except asyncio.TimeoutError:
                await interaction.followup.send("⌛ Category creation timed out", ephemeral=True)
                return
        else:
            category = category_view.category
        
        # Step 2: Topic Selection
        topics = await get_distinct_topics(category)
        
        topic_view = TopicSelect(topics)
        await category_msg.edit(content=f"**📝 Select a topic in '{category}'**", view=topic_view)
        
        # Wait for topic selection
        await topic_view.wait()
        if not topic_view.topic:
            await category_msg.edit(content="⏱️ Topic selection timed out", view=None)
            return
            
        # Handle new topic creation
        if topic_view.topic == "__new__":
            await category_msg.edit(content="⌛ Waiting for new topic...", view=None)
            
            try:
                await interaction.followup.send("Please enter a name for the new topic:", ephemeral=True)
                response = await bot.wait_for('message', timeout=60.0, check=check)
                topic = response.content.strip()
                await response.delete()
            except asyncio.TimeoutError:
                await interaction.followup.send("⌛ Topic creation timed out", ephemeral=True)
                return
        else:
            topic = topic_view.topic
        
        # Update entry
        entry.category = category
        entry.topic = topic
        entry.last_updated = datetime.now(timezone.utc)
        await entry.save()
        
        # Update view
        await interaction.followup.send(
            f"✅ Entry moved to **{category} → {topic}**",
            ephemeral=True
        )
        await self.update_embed()
        await category_msg.delete()

    @ui.button(emoji="🗑️", style=discord.ButtonStyle.red)
    async def delete_btn(self, interaction: discord.Interaction, button: ui.Button):
        entry = self.entries[self.page]
        
        MOD_ROLE_ID = int(os.getenv("MOD_ROLE_ID", 0))
        is_author = interaction.user.id == entry.author_id
        is_mod = MOD_ROLE_ID and any(role.id == MOD_ROLE_ID for role in interaction.user.roles)
        
        if not (is_author or is_mod):
            await interaction.response.send_message(
                "❌ You can only delete your own entries", 
                ephemeral=True
            )
            return
            
        await entry.delete()
        self.entries.pop(self.page)
        
        if not self.entries:
            await interaction.response.edit_message(
                content=f"✅ Entry deleted",
                embed=None,
                view=None
            )
            return
            
        if self.page >= len(self.entries):
            self.page = max(0, len(self.entries) - 1)
            
        await interaction.response.send_message("✅ Entry deleted", ephemeral=True)
        await self.update_embed()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            await self.message.edit(view=self)

# ======================
# EDIT MODAL (CONTENT ONLY)
# ======================
class EditModal(ui.Modal, title="Edit Entry Content"):
    new_description = ui.TextInput(label="Description", style=discord.TextStyle.long, required=True)
    new_image = ui.TextInput(label="Image URL", style=discord.TextStyle.short, required=False)
    new_reference = ui.TextInput(label="Reference", style=discord.TextStyle.short, required=False)
    
    def __init__(self, entry: TypologyEntry):
        super().__init__()
        self.entry = entry
        self.new_description.default = entry.description
        self.new_image.default = entry.image_url
        self.new_reference.default = entry.reference
    
    async def on_submit(self, interaction: discord.Interaction):
        self.entry.description = str(self.new_description)
        self.entry.image_url = str(self.new_image)
        self.entry.reference = str(self.new_reference)
        self.entry.last_updated = datetime.now(timezone.utc)
        await self.entry.save()
        await interaction.response.send_message("✅ Content updated!", ephemeral=True)

# ======================
# BOT SETUP
# ======================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            print(f"🔄 Database initialization attempt {attempt}/{max_retries}")
            await initialize_database()
            print(f"✅ Bot ready as {bot.user}")
            break
        except Exception as e:
            print(f"❌ Attempt {attempt} failed: {str(e)}")
            if attempt == max_retries:
                print("💥 Failed to initialize database after 3 attempts")
                raise
            await asyncio.sleep(2 ** attempt)

# ======================
# DEFINITION CREATION FLOW
# ======================
async def create_definition_flow(message: discord.Message, title: str):
    try:
        # Step 1: Category Selection
        categories = await get_distinct_categories()
        category_view = CategorySelect(categories)
        category_msg = await message.channel.send(f"**📂 Select a category for '{title}'**", view=category_view)
        
        # Wait for category selection
        await category_view.wait()
        if not category_view.category:
            await category_msg.edit(content="⏱️ Category selection timed out", view=None)
            return
            
        # Handle new category creation
        if category_view.category == "__new__":
            await category_msg.edit(content="⌛ Waiting for new category...", view=None)
            await message.channel.send("Please enter a name for the new category:", view=None)
            
            def check(m):
                return m.author == message.author and m.channel == message.channel
                
            try:
                response = await bot.wait_for('message', timeout=60.0, check=check)
                category = response.content.strip()
                await response.delete()
            except asyncio.TimeoutError:
                await message.channel.send("⌛ Category creation timed out")
                return
        else:
            category = category_view.category
        
        # Step 2: Topic Selection
        topics = await get_distinct_topics(category)
        
        topic_view = TopicSelect(topics)
        await category_msg.edit(content=f"**📝 Select a topic in '{category}'**", view=topic_view)
        
        # Wait for topic selection
        await topic_view.wait()
        if not topic_view.topic:
            await category_msg.edit(content="⏱️ Topic selection timed out", view=None)
            return
            
        # Handle new topic creation
        if topic_view.topic == "__new__":
            await category_msg.edit(content="⌛ Waiting for new topic...", view=None)
            await message.channel.send("Please enter a name for the new topic:", view=None)
            
            try:
                response = await bot.wait_for('message', timeout=60.0, check=check)
                topic = response.content.strip()
                await response.delete()
            except asyncio.TimeoutError:
                await message.channel.send("⌛ Topic creation timed out")
                return
        else:
            topic = topic_view.topic
        
        # Step 3: Get content from replied message
        if not message.reference:
            await message.channel.send("❌ Please reply to a message with the content")
            return
            
        try:
            content_msg = await message.channel.fetch_message(message.reference.message_id)
            if content_msg.author.bot:
                await message.channel.send("❌ Cannot use bot messages as content")
                return
                
            description = content_msg.content
        except:
            await message.channel.send("❌ Failed to fetch content message")
            return
            
        # Step 4: Create entry
        entry = TypologyEntry(
            title=title,
            category=category,
            topic=topic,
            description=description,
            author_id=message.author.id,
            author_name=message.author.display_name
        )
        await entry.insert()
        
        # Send confirmation
        embed = discord.Embed(
            title=f"✅ Entry created: {title}",
            description=f"**{category} → {topic}**\n{description[:200]}...",
            color=0x00ff00
        )
        embed.add_field(name="Full Content", value=f"[Jump to Message]({content_msg.jump_url})")
        await message.channel.send(embed=embed)
        await category_msg.delete()
        
    except Exception as e:
        await message.channel.send(f"❌ Error in creation flow: {str(e)}")

# ======================
# MESSAGE HANDLING
# ======================
@bot.event
async def on_message(message):
    await bot.process_commands(message)
    
    if message.content.lower().startswith("tp define") and not message.author.bot:
        try:
            parts = message.content.split(maxsplit=2)
            if len(parts) < 2:
                await message.channel.send("❌ Format: `tp define TITLE`")
                return
                
            title = parts[1].strip()
            await create_definition_flow(message, title)
            
        except Exception as e:
            await message.channel.send(f"❌ Error: {str(e)}")

# ======================
# COMMANDS
# ======================
@bot.command()
async def define(ctx, *, title: str = None):
    """Browse entries through a hierarchical interface"""
    try:
        if not title:
            # Start hierarchical selection
            categories = await get_distinct_categories()
            if not categories:
                await ctx.send("❌ No categories found")
                return
                
            view = CategorySelect(categories)
            msg = await ctx.send("📂 **Select a category:**", view=view)
            await view.wait()
            
            if not view.category or view.category == "__new__":
                await msg.edit(content="❌ Category selection cancelled", view=None)
                return
                
            # Get topics for selected category
            topics = await get_distinct_topics(view.category)
            
            if not topics:
                await msg.edit(content=f"❌ No topics found for {view.category}", view=None)
                return
                
            topic_view = TopicSelect(topics)
            await msg.edit(content=f"📝 **Select a topic in {view.category}:**", view=topic_view)
            await topic_view.wait()
            
            if not topic_view.topic or topic_view.topic == "__new__":
                await msg.edit(content="❌ Topic selection cancelled", view=None)
                return
                
            # Get entries for selected topic
            entries = await TypologyEntry.find(
                TypologyEntry.category == view.category,
                TypologyEntry.topic == topic_view.topic
            ).sort(-TypologyEntry.votes).to_list()
            
            if not entries:
                await msg.edit(content=f"❌ No entries found for {view.category}/{topic_view.topic}", view=None)
                return
                
            # Show entries
            entry_view = EntryView(entries, ctx.author.id)
            entry_view.message = await ctx.send(
                embed=discord.Embed(
                    title=f"Loading entries for {topic_view.topic}...",
                    color=0x6A0DAD
                ),
                view=entry_view
            )
            await entry_view.update_embed()
            await msg.delete()
            
        else:
            # Direct title search
            entries = await TypologyEntry.find(
                TypologyEntry.title == title
            ).sort(-TypologyEntry.votes).to_list()
            
            if not entries:
                await ctx.send(f"❌ No entries found for '{title}'")
                return
                
            view = EntryView(entries, ctx.author.id)
            view.message = await ctx.send(
                embed=discord.Embed(
                    title=f"Loading entries for '{title}'...",
                    color=0x6A0DAD
                ),
                view=view
            )
            await view.update_embed()
            
    except Exception as e:
        await ctx.send(f"❌ Error: {str(e)}")

@bot.command()
async def search(ctx, *, query: str):
    """Search across all entries"""
    try:
        results = await TypologyEntry.find(
            {"$text": {"$search": query}}
        ).sort(-TypologyEntry.votes).to_list()
        
        if not results:
            await ctx.send("🔍 No results found")
            return
            
        # Create view with search results
        view = EntryView(results, ctx.author.id)
        view.message = await ctx.send(
            embed=discord.Embed(
                title=f"Search results for: {query}",
                color=0x3498DB
            ),
            view=view
        )
        await view.update_embed()
    except Exception as e:
        await ctx.send(f"❌ Search error: {str(e)}")

# ======================
# START BOT
# ======================
if __name__ == "__main__":
    required_vars = ["DISCORD_TOKEN", "MONGO_URI"]
    if missing := [var for var in required_vars if not os.getenv(var)]:
        print(f"❌ Missing environment variables: {', '.join(missing)}")
        exit(1)
    
    if not os.getenv("MOD_ROLE_ID"):
        print("⚠️ MOD_ROLE_ID not set - moderator deletion disabled")
    
    try:
        bot.run(os.getenv("DISCORD_TOKEN"))
    except Exception as e:
        print(f"💥 Bot crashed: {str(e)}")